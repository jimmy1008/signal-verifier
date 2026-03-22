"""
全自動交易腳本

使用方式：
    python scripts/auto_trade.py
    python scripts/auto_trade.py --dry-run        # 模擬模式（不實際下單）
    python scripts/auto_trade.py --risk 0.01      # 每筆風險 1%
    python scripts/auto_trade.py --max-positions 3 # 最多同時 3 倉

流程：
    1. 連線 Telegram + OANDA
    2. 監聽信號頻道新訊息
    3. 解析信號 → 計算倉位 → 下單
    4. 監控持倉 → TP1 保本 → TP3 出場
    5. 記錄所有操作

策略：TP3 出場 + 碰 TP1 後 SL 移到 Entry
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient, events
from telethon.tl.types import Message

from src.config import load_config
from src.database import init_db, get_session
from src.models import RawMessageORM, SignalSide
from src.parsers.registry import get_parser
from src.trader.base import BaseBroker
from src.trader.executor import TradeExecutor, ExecutorConfig
import httpx


async def notify_error(config, msg: str):
    """出錯時通過 Bot 發通知（支援 HTML 格式）"""
    try:
        notify_cfg = config.get("notify", {})
        bot_token = notify_cfg.get("bot_token", "")
        chat_ids = notify_cfg.get("chat_ids", [])
        if not bot_token or not chat_ids:
            return
        async with httpx.AsyncClient() as hc:
            for cid in chat_ids:
                await hc.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
                    timeout=10,
                )
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("auto_trade.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# Dry Run Broker（模擬模式）
# ============================================================

class DryRunBroker(BaseBroker):
    """不實際下單，只打 log"""

    def __init__(self, initial_balance: float = 100.0):
        self.balance = initial_balance
        self.trades: dict[str, dict] = {}
        self._trade_counter = 0

    async def connect(self) -> bool:
        logger.info("[DRY RUN] Broker 已連線（模擬模式）")
        return True

    async def get_account(self):
        from src.trader.base import AccountInfo
        return AccountInfo(
            balance=self.balance,
            equity=self.balance,
            open_positions=len(self.trades),
        )

    async def get_price(self, symbol: str) -> tuple[float, float]:
        # 模擬模式無法取得即時報價，用 0
        return 0.0, 0.0

    async def market_order(self, symbol, side, units, sl=None, tp=None, tp3=None):
        from src.trader.base import OrderResult
        self._trade_counter += 1
        trade_id = f"DRY_{self._trade_counter}"

        self.trades[trade_id] = {
            "symbol": symbol, "side": side.value, "units": units,
            "sl": sl, "tp": tp,
        }

        logger.info(
            f"[DRY RUN] 下單: {symbol} {side.value} "
            f"units={units} SL={sl} TP={tp}"
        )

        return OrderResult(
            success=True,
            order_id=trade_id,
            trade_id=trade_id,
            symbol=symbol,
            side=side.value,
            units=units,
            sl_price=sl or 0,
            tp_price=tp or 0,
        )

    async def modify_trade(self, trade_id, sl=None, tp=None, **kwargs) -> bool:
        logger.info(f"[DRY RUN] 修改 {trade_id}: SL={sl} TP={tp}")
        if trade_id in self.trades:
            if sl: self.trades[trade_id]["sl"] = sl
            if tp: self.trades[trade_id]["tp"] = tp
        return True

    async def close_trade(self, trade_id) -> bool:
        logger.info(f"[DRY RUN] 平倉 {trade_id}")
        self.trades.pop(trade_id, None)
        return True

    async def get_open_positions(self):
        from src.trader.base import Position
        return [
            Position(
                trade_id=tid,
                symbol=t["symbol"],
                side=t["side"],
                units=t["units"],
                entry_price=0,
                sl_price=t.get("sl"),
                tp_price=t.get("tp"),
            )
            for tid, t in self.trades.items()
        ]

    async def get_position_by_symbol(self, symbol):
        for tid, t in self.trades.items():
            if t["symbol"] == symbol:
                from src.trader.base import Position
                return Position(
                    trade_id=tid, symbol=symbol,
                    side=t["side"], units=t["units"],
                    entry_price=0,
                )
        return None


# ============================================================
# Main
# ============================================================

async def main():
    arg_parser = argparse.ArgumentParser(description="全自動交易")
    arg_parser.add_argument("--dry-run", action="store_true", help="模擬模式（不下單）")
    _default_risk = load_config().get("trading", {}).get("risk_per_trade", 0.02)
    arg_parser.add_argument("--risk", type=float, default=_default_risk, help="每筆風險比例（從 config 讀取）")
    arg_parser.add_argument("--max-positions", type=int, default=0, help="最大持倉數（0=無上限）")
    args = arg_parser.parse_args()

    config = load_config()
    init_db(config["database"]["url"])

    # ─── 初始化 Broker ───
    if args.dry_run:
        broker = DryRunBroker()
        logger.info(">>> 模擬模式 <<<")
    else:
        from src.trader.router import TradingRouter

        # BingX（加密貨幣）— H1 主帳戶 + H4 子帳戶
        crypto_broker = None
        crypto_broker_h4 = None
        bingx_cfg = config.get("bingx", {})
        if bingx_cfg.get("api_key") and bingx_cfg["api_key"] != "YOUR_BINGX_API_KEY":
            from src.trader.bingx import BingXBroker
            crypto_broker = BingXBroker(
                api_key=bingx_cfg["api_key"],
                api_secret=bingx_cfg["api_secret"],
                is_demo=bingx_cfg.get("is_demo", True),
                leverage=bingx_cfg.get("leverage", 20),
                margin_mode="isolated",
            )
            logger.info("BingX H1 主帳戶已設定")

            # H4 子帳戶
            if bingx_cfg.get("sub_api_key"):
                crypto_broker_h4 = BingXBroker(
                    api_key=bingx_cfg["sub_api_key"],
                    api_secret=bingx_cfg["sub_api_secret"],
                    is_demo=bingx_cfg.get("is_demo", True),
                    leverage=bingx_cfg.get("leverage", 20),
                    margin_mode="isolated",
                )
                logger.info("BingX H4 子帳戶已設定")

        # OANDA（外匯 / CFD）
        forex_broker = None
        oanda_cfg = config.get("oanda", {})
        if oanda_cfg.get("api_token") and oanda_cfg["api_token"] != "YOUR_OANDA_API_TOKEN":
            from src.trader.oanda import OandaBroker
            forex_broker = OandaBroker(
                api_token=oanda_cfg["api_token"],
                account_id=oanda_cfg["account_id"],
                is_live=oanda_cfg.get("is_live", False),
            )
            logger.info("OANDA 已設定（外匯 / CFD）")

        if not crypto_broker and not forex_broker:
            logger.error("請在 config.yaml 設定至少一個 broker（BingX 或 OANDA）")
            logger.error("或使用 --dry-run 模擬模式")
            return

        broker_h1 = TradingRouter(crypto_broker=crypto_broker, forex_broker=forex_broker)
        broker_h4 = TradingRouter(crypto_broker=crypto_broker_h4, forex_broker=forex_broker) if crypto_broker_h4 else broker_h1
        if not forex_broker:
            logger.info("注意：外匯信號將被跳過（OANDA 未設定）")

    connected = await broker_h1.connect()
    if not connected:
        logger.error("H1 Broker 連線失敗")
        return

    if broker_h4 is not broker_h1:
        connected_h4 = await broker_h4.connect()
        if not connected_h4:
            logger.error("H4 Broker 連線失敗")
            return

    # 顯示帳戶資訊
    if hasattr(broker_h1, "get_combined_account"):
        accounts = await broker_h1.get_combined_account()
        for name, info in accounts.items():
            logger.info(f"[H1 {name.upper()}] 餘額: {info['balance']:,.2f} {info['currency']}")
    if broker_h4 is not broker_h1 and hasattr(broker_h4, "get_combined_account"):
        accounts = await broker_h4.get_combined_account()
        for name, info in accounts.items():
            logger.info(f"[H4 {name.upper()}] 餘額: {info['balance']:,.2f} {info['currency']}")

    # ─── 初始化執行器（H1 + H4）───
    exec_config = ExecutorConfig(
        risk_per_trade=args.risk,
        max_positions=args.max_positions,
    )

    executor_h1 = TradeExecutor(broker_h1, exec_config, label="h1")
    executor_h4 = TradeExecutor(broker_h4, exec_config, label="h4")

    def _get_executor(timeframe: str) -> TradeExecutor:
        """根據 timeframe 選擇 executor"""
        if timeframe and "4" in timeframe:
            return executor_h4
        return executor_h1

    logger.info("策略: TP4 出場, 分批 25%, 碰 TP1 保本")
    logger.info(f"風險: {args.risk:.1%} / 筆, 最大持倉: {'無上限' if args.max_positions == 0 else args.max_positions}")
    if broker_h4 is not broker_h1:
        logger.info("雙帳戶模式: H1→主帳戶, H4→子帳戶")
    if args.dry_run:
        logger.info(">>> 模擬模式 <<<")

    # ─── 連線 Telegram ───
    tg_cfg = config["telegram"]
    tg_client = TelegramClient(
        tg_cfg["session_name"],
        tg_cfg["api_id"],
        tg_cfg["api_hash"],
    )
    await tg_client.start(phone=tg_cfg["phone"])
    logger.info("Telegram 已連線")

    # 取得頻道設定
    channels = tg_cfg["channels"]
    chat_ids = [ch["chat_id"] for ch in channels]
    parser_map = {ch["chat_id"]: ch.get("parser", "default") for ch in channels}

    # ─── 啟動持倉監控 ───
    if not args.dry_run:
        await executor_h1.start_monitor()
        if executor_h4 is not executor_h1:
            await executor_h4.start_monitor()

    # ─── 監聽新訊息 ───
    @tg_client.on(events.NewMessage(chats=chat_ids))
    async def on_signal(event: events.NewMessage.Event):
        msg = event.message
        if not msg.text:
            return

        # 存入 DB
        session = get_session()
        raw_id = None
        try:
            raw = RawMessageORM(
                source=str(msg.chat_id),
                chat_id=str(msg.chat_id),
                message_id=str(msg.id),
                timestamp=msg.date.replace(tzinfo=None) if msg.date else None,
                raw_text=msg.text,
                parsed_status="pending",
            )
            session.add(raw)
            session.commit()
            raw_id = raw.id
        finally:
            session.close()

        # 解析
        parser_name = parser_map.get(msg.chat_id, "default")
        parser = get_parser(parser_name)
        timestamp = msg.date.replace(tzinfo=None) if msg.date else None
        if timestamp is None:
            timestamp = datetime.utcnow()
        parsed = parser.parse(msg.text, timestamp)

        # 更新解析狀態
        if raw_id:
            session = get_session()
            try:
                raw_row = session.query(RawMessageORM).get(raw_id)
                if raw_row:
                    if parsed:
                        raw_row.parsed_status = "parsed"
                        # 關聯 raw_message_id
                        parsed.raw_message_id = raw_id
                    else:
                        raw_row.parsed_status = "skipped"
                    session.commit()
            except Exception:
                pass
            finally:
                session.close()

        if parsed is None:
            return

        if parsed.signal_type == "entry":
            tf = parsed.timeframe or "1h"
            executor = _get_executor(tf)
            acct = "H4子帳戶" if "4" in tf else "H1主帳戶"
            logger.info(f"[新信號] {parsed.symbol} {parsed.side.value} @ {parsed.entry} ({tf} → {acct})")

            # 存入 signals 表
            try:
                from src.models import SignalORM, SignalStatus
                session = get_session()
                sig_key = parsed.related_signal_key or f"{parsed.symbol}_{parsed.side.value}_{tf}_{parsed.entry}"
                existing = session.query(SignalORM).filter_by(signal_key=sig_key).first()
                if not existing:
                    sig_row = SignalORM(
                        source=str(msg.chat_id),
                        signal_key=sig_key,
                        symbol=parsed.symbol,
                        side=parsed.side,
                        entry=parsed.entry,
                        sl=parsed.sl,
                        tp1=parsed.tp1,
                        tp2=parsed.tp2,
                        tp3=parsed.tp3,
                        tp4=parsed.tp4,
                        timeframe=tf,
                        signal_time=timestamp,
                        raw_message_id=raw_id,
                        status=SignalStatus.PENDING,
                    )
                    session.add(sig_row)
                    session.commit()
                session.close()
            except Exception as e:
                logger.debug(f"存 signal 失敗: {e}")

            try:
                result = await executor.execute_signal(parsed)
                if result and result.success:
                    logger.info(f"[已進場] {result.symbol} trade_id={result.trade_id}")
                    # 檢查保護單是否都掛上
                    if hasattr(executor.broker, '_last_protection_failure') and executor.broker._last_protection_failure:
                        pf = executor.broker._last_protection_failure
                        alert = (
                            f"⚠️ <b>保護單缺失</b>\n\n"
                            f"幣種: <code>{pf['symbol']}</code>\n"
                            f"方向: <code>{pf['side']}</code>\n"
                            f"級別: <code>{tf}</code> → {acct}\n"
                            f"缺少: <code>{', '.join(pf['missing'])}</code>\n\n"
                            f"請手動檢查補掛！"
                        )
                        await notify_error(config, alert)
                        executor.broker._last_protection_failure = None
                elif result and not result.success:
                    logger.error(f"[下單失敗] {parsed.symbol}: {result.error}")
                    # 推送下單失敗通知
                    risk_pct = config.get("trading", {}).get("risk_per_trade", 0.01) * 100
                    try:
                        acct_info = await executor.broker.get_account()
                        margin_str = f"${acct_info.balance * risk_pct / 100:.2f}"
                        balance_str = f"${acct_info.balance:.2f}"
                    except Exception:
                        margin_str = "-"
                        balance_str = "-"
                    alert = (
                        f"⚠️ <b>下單失敗</b>\n\n"
                        f"幣種: <code>{parsed.symbol}</code>\n"
                        f"方向: <code>{parsed.side.value.upper()}</code>\n"
                        f"級別: <code>{tf}</code>\n"
                        f"帳戶: <code>{acct}</code>\n"
                        f"餘額: <code>{balance_str}</code>\n"
                        f"風險金: <code>{margin_str}</code> ({risk_pct:.1f}%)\n"
                        f"Entry: <code>{parsed.entry}</code>\n"
                        f"SL: <code>{parsed.sl}</code>\n\n"
                        f"原因: <code>{result.error}</code>"
                    )
                    await notify_error(config, alert)
            except ValueError:
                # CFD 信號沒有 broker，靜默跳過
                pass
        elif parsed.signal_type in ("update", "close", "cancel"):
            logger.info(f"[頻道回報] {parsed.update_type}: {parsed.update_value} (key={parsed.related_signal_key})")

            # 存入 signal_updates 表
            if parsed.related_signal_key and parsed.update_type:
                try:
                    from src.models import SignalUpdateORM, SignalORM
                    session = get_session()
                    sig = session.query(SignalORM).filter_by(signal_key=parsed.related_signal_key).first()
                    if sig:
                        update_row = SignalUpdateORM(
                            signal_id=sig.id,
                            update_type=parsed.update_type,
                            update_value=parsed.update_value,
                            raw_message_id=raw_id,
                            timestamp=timestamp,
                        )
                        session.add(update_row)
                        session.commit()
                    session.close()
                except Exception as e:
                    logger.debug(f"存 update 失敗: {e}")

            # 從 key 判斷 timeframe（如 BTCUSDTP4H032020 → 4h）
            key = parsed.related_signal_key or ""
            tf = "4h" if "4H" in key.upper() else "1h"
            executor = _get_executor(tf)
            await executor.handle_update(parsed)

    # ─── 定時補抓（防漏訊息）───
    async def poll_missed():
        """每 30 秒檢查最近 5 分鐘內的訊息，補抓漏掉的"""
        from datetime import timedelta
        seen_ids = set()

        while True:
            try:
                await asyncio.sleep(30)
                cutoff = datetime.utcnow() - timedelta(minutes=5)

                for ch in channels:
                    chat_id = ch["chat_id"]
                    parser_name = ch.get("parser", "default")

                    async for msg in tg_client.iter_messages(chat_id, limit=20):
                        if not msg.text:
                            continue
                        if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                            break

                        # 用 chat_id + message_id 去重
                        uid = f"{chat_id}_{msg.id}"
                        if uid in seen_ids:
                            continue
                        seen_ids.add(uid)

                        # 檢查 DB 是否已存
                        session = get_session()
                        try:
                            exists = session.query(RawMessageORM).filter_by(
                                chat_id=str(chat_id), message_id=str(msg.id)
                            ).first()
                            if exists:
                                continue

                            # 漏掉的訊息！補存 + 補解析
                            raw = RawMessageORM(
                                source=str(chat_id),
                                chat_id=str(chat_id),
                                message_id=str(msg.id),
                                timestamp=msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow(),
                                raw_text=msg.text,
                                parsed_status="pending",
                            )
                            session.add(raw)
                            session.commit()

                            logger.warning(f"[補抓] 漏掉的訊息 msg_id={msg.id}")

                            # 解析並執行
                            parser = get_parser(parser_name)
                            ts = msg.date.replace(tzinfo=None) if msg.date else datetime.utcnow()
                            parsed = parser.parse(msg.text, ts)

                            if parsed and parsed.signal_type == "entry":
                                tf = parsed.timeframe or "1h"
                                executor = _get_executor(tf)
                                acct = "H4子帳戶" if "4" in tf else "H1主帳戶"
                                logger.info(f"[補抓] 新信號: {parsed.symbol} {parsed.side.value} @ {parsed.entry} ({tf})")
                                result = await executor.execute_signal(parsed)
                                if result and not result.success:
                                    logger.error(f"[補抓下單失敗] {parsed.symbol}: {result.error}")
                                    risk_pct = config.get("trading", {}).get("risk_per_trade", 0.01) * 100
                                    alert = (
                                        f"⚠️ <b>下單失敗（補抓）</b>\n\n"
                                        f"幣種: <code>{parsed.symbol}</code>\n"
                                        f"方向: <code>{parsed.side.value.upper()}</code>\n"
                                        f"級別: <code>{tf}</code> → {acct}\n"
                                        f"Entry: <code>{parsed.entry}</code> | SL: <code>{parsed.sl}</code>\n\n"
                                        f"原因: <code>{result.error}</code>"
                                    )
                                    await notify_error(config, alert)
                            elif parsed and parsed.signal_type in ("update", "close", "cancel"):
                                key = parsed.related_signal_key or ""
                                tf = "4h" if "4H" in key.upper() else "1h"
                                executor = _get_executor(tf)
                                logger.info(f"[補抓] 更新: {parsed.update_type} {parsed.update_value} ({tf})")
                                await executor.handle_update(parsed)
                        finally:
                            session.close()

                # 清理太舊的 seen_ids（防記憶體膨脹）
                if len(seen_ids) > 5000:
                    seen_ids.clear()

            except Exception as e:
                logger.error(f"[補抓] 異常: {e}")

    poll_task = asyncio.create_task(poll_missed())

    # ─── 漏跳通知 ───
    async def notify_missed_loop():
        """定時檢查漏跳信號，發 Telegram 通知"""
        notify_cfg = config.get("notify", {})
        bot_token = notify_cfg.get("bot_token", "")
        notify_chats = notify_cfg.get("chat_ids", [])
        # 相容舊格式 chat_id（單一）
        if not notify_chats and notify_cfg.get("chat_id"):
            notify_chats = [notify_cfg["chat_id"]]
        interval = notify_cfg.get("check_interval", 300)
        timeout_h = notify_cfg.get("timeout_hours", 6)

        if not bot_token or bot_token == "YOUR_BOT_TOKEN" or not notify_chats:
            logger.info("漏跳通知未設定，跳過")
            return

        import httpx
        from src.models import SignalUpdateORM

        logger.info(f"漏跳通知已啟動（每 {interval} 秒，超 {timeout_h}h 通知）")
        notified_keys = set()  # 已通知過的，防重複

        while True:
            try:
                await asyncio.sleep(interval)
                from datetime import timedelta
                session = get_session()
                cutoff = datetime.utcnow() - timedelta(hours=timeout_h)
                min_age = datetime.utcnow() - timedelta(hours=1)

                from src.models import SignalORM
                sigs = (
                    session.query(SignalORM)
                    .filter(SignalORM.signal_time > cutoff, SignalORM.signal_time < min_age)
                    .all()
                )

                missed = []
                for sig in sigs:
                    if sig.signal_key in notified_keys:
                        continue
                    ups = session.query(SignalUpdateORM).filter_by(signal_id=sig.id).count()
                    if ups == 0:
                        missed.append(sig)
                        notified_keys.add(sig.signal_key)

                session.close()

                if missed:
                    lines = [f"<b>SignalJudge - {len(missed)} 筆信號無回報</b>\n"]
                    for sig in missed[:10]:
                        age = (datetime.utcnow() - sig.signal_time).total_seconds() / 3600
                        lines.append(f"  {sig.symbol} {sig.side.value.upper()} @ {sig.entry} ({age:.1f}h)")
                    if len(missed) > 10:
                        lines.append(f"\n  ...還有 {len(missed) - 10} 筆")

                    text = "\n".join(lines)
                    async with httpx.AsyncClient() as hc:
                        for cid in notify_chats:
                            try:
                                await hc.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                                )
                            except Exception:
                                pass
                    logger.info(f"[漏跳通知] 已發送 {len(missed)} 筆 → {len(notify_chats)} 人")

                # 清理太舊的
                if len(notified_keys) > 5000:
                    notified_keys.clear()

            except Exception as e:
                logger.error(f"[漏跳通知] 異常: {e}")

    notify_task = asyncio.create_task(notify_missed_loop())

    # ─── 定時狀態報告（台灣 08:00~22:00 每 4 小時）───
    async def status_report():
        """台灣時間 08, 12, 16, 20 點發狀態"""
        notify_cfg = config.get("notify", {})
        bot_token = notify_cfg.get("bot_token", "")
        notify_chats = notify_cfg.get("chat_ids", [])
        if not bot_token or not notify_chats:
            return

        # 台灣 08,12,16,20 = UTC 00,04,08,12
        report_hours = {0, 4, 8, 12}
        last_report = None

        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.utcnow()
                report_key = f"{now.date()}_{now.hour}"

                if now.hour in report_hours and now.minute < 2 and last_report != report_key:
                    last_report = report_key
                    tw_hour = (now.hour + 8) % 24

                    from datetime import timedelta
                    session = get_session()
                    from src.models import SignalORM

                    today_start = now.replace(hour=0, minute=0, second=0)
                    cfd_count = session.query(SignalORM).filter(
                        SignalORM.signal_time > today_start,
                        SignalORM.source == "CRT_SNIPER_CFD",
                    ).count()
                    crypto_count = session.query(SignalORM).filter(
                        SignalORM.signal_time > today_start,
                        SignalORM.source == "CRT_SNIPER_CRYPTO",
                    ).count()

                    # 已結單（今天 closed 的）
                    closed_trades = (
                        sum(1 for s in executor_h1.active_trades.values() if s.closed)
                        + sum(1 for s in executor_h4.active_trades.values() if s.closed)
                    )
                    active = executor_h1.get_trade_count() + executor_h4.get_trade_count()

                    # 今日漏跳
                    from src.models import SignalUpdateORM
                    today_sigs = session.query(SignalORM).filter(SignalORM.signal_time > today_start).all()
                    no_update = sum(1 for sig in today_sigs
                                    if session.query(SignalUpdateORM).filter_by(signal_id=sig.id).count() == 0
                                    and (now - sig.signal_time).total_seconds() > 3600)

                    session.close()

                    # 下次通知
                    next_hours = sorted(report_hours)
                    next_utc = None
                    for h in next_hours:
                        if h > now.hour:
                            next_utc = h
                            break
                    if next_utc is None:
                        next_utc = next_hours[0]
                    next_tw = (next_utc + 8) % 24

                    text = (
                        f"<b>SignalJudge {tw_hour:02d}:00</b>\n\n"
                        f"運行: 正常\n"
                        f"今日信號: Crypto {crypto_count} / CFD {cfd_count}\n"
                        f"持倉中: {active} | 已結單: {closed_trades}\n"
                        f"漏跳偵測: {no_update} 筆無回報\n"
                        f"\n下次通知: {next_tw:02d}:00"
                    )

                    async with httpx.AsyncClient() as hc:
                        for cid in notify_chats:
                            try:
                                await hc.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                                )
                            except Exception:
                                pass
                    logger.info(f"[狀態報告] {tw_hour:02d}:00 已發送")

            except Exception as e:
                logger.error(f"[狀態報告] 異常: {e}")

    summary_task = asyncio.create_task(status_report())

    logger.info(f"開始監聽 {len(chat_ids)} 個頻道...")
    logger.info("補抓機制已啟動（每 30 秒檢查漏訊息）")
    logger.info("按 Ctrl+C 停止")

    try:
        await tg_client.run_until_disconnected()
    finally:
        logger.info("開始優雅關機...")

        # 1. 停止接收新信號
        poll_task.cancel()
        notify_task.cancel()
        summary_task.cancel()

        # 2. 停止持倉監控
        await executor_h1.stop_monitor()
        if executor_h4 is not executor_h1:
            await executor_h4.stop_monitor()

        # 3. 存檔所有狀態
        executor_h1._save_state()
        if executor_h4 is not executor_h1:
            executor_h4._save_state()
        logger.info("持倉狀態已存檔")

        # 4. 斷開連線
        await tg_client.disconnect()
        logger.info("優雅關機完成")


if __name__ == "__main__":
    asyncio.run(main())

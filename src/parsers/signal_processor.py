"""
訊號處理器

職責：
- 從 raw_messages 表讀取未解析的訊息
- 使用指定 parser 解析
- 將結果寫入 signals / signal_updates 表
- 處理信號關聯（update 訊息掛回原始信號）

輸入：raw_messages (DB)
輸出：signals / signal_updates (DB)
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from src.models import (
    RawMessageORM,
    SignalORM,
    SignalUpdateORM,
    ParsedSignal,
    SignalStatus,
)
from src.parsers.base import BaseParser

logger = logging.getLogger(__name__)


def process_pending_messages(session: Session, parser: BaseParser, source: Optional[str] = None) -> int:
    """
    處理所有 pending 狀態的原始訊息。

    Returns:
        成功解析的信號數量
    """
    query = session.query(RawMessageORM).filter_by(parsed_status="pending")
    if source:
        query = query.filter_by(source=source)
    query = query.order_by(RawMessageORM.timestamp.asc())

    messages = query.all()
    parsed_count = 0

    for msg in messages:
        result = parser.parse(msg.raw_text, msg.timestamp, msg.id)

        if result is None:
            msg.parsed_status = "skipped"
            continue

        if result.signal_type == "entry":
            _save_entry_signal(session, result, msg)
            parsed_count += 1
        elif result.signal_type in ("update", "close", "cancel"):
            _save_update(session, result, msg)

        msg.parsed_status = "parsed"

    session.commit()
    logger.info(f"處理 {len(messages)} 則訊息，解析出 {parsed_count} 筆新信號")
    return parsed_count


def _save_entry_signal(session: Session, parsed: ParsedSignal, raw: RawMessageORM) -> None:
    """儲存進場信號"""
    # 建立 signal_key：優先用 parser 提供的 key（如 CRT SNIPER 的 #NAS100USD4H031709）
    if parsed.related_signal_key:
        signal_key = f"{raw.source}_{parsed.related_signal_key}"
    else:
        signal_key = f"{raw.source}_{parsed.symbol}_{parsed.side.value}_{parsed.entry}_{raw.message_id}"

    existing = session.query(SignalORM).filter_by(signal_key=signal_key).first()
    if existing:
        logger.debug(f"信號已存在: {signal_key}")
        return

    signal = SignalORM(
        source=raw.source,
        signal_key=signal_key,
        symbol=parsed.symbol,
        side=parsed.side,
        entry=parsed.entry,
        sl=parsed.sl,
        tp1=parsed.tp1,
        tp2=parsed.tp2,
        tp3=parsed.tp3,
        tp4=parsed.tp4,
        timeframe=parsed.timeframe,
        signal_time=parsed.signal_time,
        raw_message_id=raw.id,
        status=SignalStatus.PENDING,
    )
    session.add(signal)
    session.flush()  # 取得 id
    logger.info(f"新信號: {parsed.symbol} {parsed.side.value} @ {parsed.entry}")


def _save_update(session: Session, parsed: ParsedSignal, raw: RawMessageORM) -> None:
    """
    儲存更新訊息。
    嘗試透過 reply_to_message_id 關聯到原始信號。
    如果無法關聯，嘗試找最近的同 symbol 信號。
    """
    signal_id = None

    # 方法 1：透過 signal_key 關聯（CRT SNIPER 用 #ID 精確匹配）
    if parsed.related_signal_key:
        key_to_find = f"{raw.source}_{parsed.related_signal_key}"
        matched = session.query(SignalORM).filter_by(signal_key=key_to_find).first()
        if matched:
            signal_id = matched.id

    # 方法 2：透過 reply_to 關聯
    if signal_id is None and raw.reply_to_message_id:
        parent_raw = (
            session.query(RawMessageORM)
            .filter_by(chat_id=raw.chat_id, message_id=raw.reply_to_message_id)
            .first()
        )
        if parent_raw:
            parent_signal = (
                session.query(SignalORM).filter_by(raw_message_id=parent_raw.id).first()
            )
            if parent_signal:
                signal_id = parent_signal.id

    # 方法 3：找最近一筆同 source 的 pending/triggered 信號
    if signal_id is None:
        recent = (
            session.query(SignalORM)
            .filter_by(source=raw.source)
            .filter(SignalORM.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]))
            .order_by(SignalORM.signal_time.desc())
            .first()
        )
        if recent:
            signal_id = recent.id

    if signal_id is None:
        logger.warning(f"無法關聯 update 訊息: {raw.raw_text[:50]}")
        return

    update = SignalUpdateORM(
        signal_id=signal_id,
        update_type=parsed.update_type,
        update_value=parsed.update_value,
        raw_message_id=raw.id,
        timestamp=parsed.signal_time,
    )
    session.add(update)
    logger.info(f"Update 已關聯到信號 #{signal_id}: {parsed.update_type}")

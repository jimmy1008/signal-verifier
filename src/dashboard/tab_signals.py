"""Tab: 信號動態 — 訊息流 + 漏跳偵測"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd
import streamlit as st

from src.database import get_session
from src.models import SignalORM, RawMessageORM, SignalUpdateORM


def tab_monitor(results, signals):
    # 頻道名稱映射
    from src.config import load_config as _lc_mon
    _mon_cfg = _lc_mon()
    _ch_map = {}
    for ch in _mon_cfg.get("telegram", {}).get("channels", []):
        _ch_map[str(ch.get("chat_id", ""))] = ch.get("name", "")

    st.markdown(
        '<span class="led led-on"></span>API 在線 &nbsp;&nbsp;'
        '<span class="led led-on"></span>DB 已連線 &nbsp;&nbsp;'
        '<span class="led led-off"></span>WebSocket 離線',
        unsafe_allow_html=True,
    )
    st.markdown("")

    # 單一 session 處理所有查詢
    s = get_session()
    try:
        total = s.query(RawMessageORM).count()
        parsed = s.query(RawMessageORM).filter_by(parsed_status="parsed").count()
        latest = s.query(RawMessageORM).order_by(RawMessageORM.timestamp.desc()).first()
        sig_count = s.query(SignalORM).count()
        update_count = s.query(SignalUpdateORM).count()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("總訊息", total)
        c2.metric("信號 / 回報", f"{sig_count} / {update_count}")
        c3.metric("解析率", f"{parsed / total * 100:.1f}%" if total else "0%")
        c4.metric("最後信號", latest.timestamp.strftime("%m/%d %H:%M") if latest else "-")

        # 只顯示已解析的訊息
        recent = s.query(RawMessageORM).filter(
            RawMessageORM.parsed_status == "parsed"
        ).order_by(RawMessageORM.timestamp.desc()).limit(30).all()

        # 批量預載關聯數據（避免 N+1 查詢）
        msg_ids = [msg.id for msg in recent]
        sigs_by_msg = {sig.raw_message_id: sig
                       for sig in s.query(SignalORM).filter(SignalORM.raw_message_id.in_(msg_ids)).all()}
        updates_by_msg = {u.raw_message_id: u
                          for u in s.query(SignalUpdateORM).filter(SignalUpdateORM.raw_message_id.in_(msg_ids)).all()}
        # 預載 update 關聯的信號
        update_sig_ids = {u.signal_id for u in updates_by_msg.values() if u.signal_id}
        sigs_by_id = {sig.id: sig
                      for sig in s.query(SignalORM).filter(SignalORM.id.in_(update_sig_ids)).all()} if update_sig_ids else {}

        rows = []
        for msg in recent:
            ch_name = _ch_map.get(str(msg.chat_id), str(msg.source)[:12])
            sig = sigs_by_msg.get(msg.id)
            update = updates_by_msg.get(msg.id) if not sig else None

            if sig:
                rows.append({
                    "時間": msg.timestamp.strftime("%m/%d %H:%M") if msg.timestamp else "",
                    "頻道": ch_name,
                    "類型": "進場",
                    "商品": sig.symbol.replace("USDT.P", "").replace(".P", ""),
                    "方向": sig.side.value.upper() if sig.side else "-",
                    "詳情": f"Entry {sig.entry} | SL {sig.sl} | TP1 {sig.tp1}",
                })
            elif update:
                rel = sigs_by_id.get(update.signal_id)
                sym = rel.symbol.replace("USDT.P", "").replace(".P", "") if rel else "-"
                ut = update.update_type.value if update.update_type else "-"
                uv = update.update_value or ""
                rows.append({
                    "時間": msg.timestamp.strftime("%m/%d %H:%M") if msg.timestamp else "",
                    "頻道": ch_name,
                    "類型": ut.upper().replace("_", " "),
                    "商品": sym,
                    "方向": rel.side.value.upper() if rel and rel.side else "-",
                    "詳情": uv,
                })
            else:
                rows.append({
                    "時間": msg.timestamp.strftime("%m/%d %H:%M") if msg.timestamp else "",
                    "頻道": ch_name,
                    "類型": "其他",
                    "商品": "-",
                    "方向": "-",
                    "詳情": msg.raw_text[:40] if msg.raw_text else "-",
                })
    finally:
        s.close()

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=420)


def tab_missed(results, signals):
    st.markdown("**頻道漏跳偵測**")
    st.caption("偵測有進場信號但缺少 TP/SL 回報的單，可能是頻道後台漏發")

    s = get_session()
    try:
        all_sigs = s.query(SignalORM).order_by(SignalORM.signal_time.desc()).all()
        all_updates = s.query(SignalUpdateORM).all()
    finally:
        s.close()

    update_by_sig = defaultdict(list)
    for u in all_updates:
        update_by_sig[u.signal_id].append(u)

    missing = []
    partial = []
    complete = []

    for sig in all_sigs:
        ups = update_by_sig.get(sig.id, [])
        tp_levels = set()
        has_sl = False

        for u in ups:
            if u.update_type and u.update_type.value == "tp_hit" and u.update_value:
                tp_levels.add(u.update_value)
            elif u.update_type and u.update_type.value in ("close_now", "cancel"):
                has_sl = True

        if "tp4" in tp_levels or has_sl:
            complete.append(sig)
        elif not ups:
            missing.append(sig)
        else:
            partial.append((sig, tp_levels))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("總信號", len(all_sigs))
    c2.metric("完整結束", len(complete))
    c3.metric("部分回報", len(partial))
    c4.metric("完全無回報", len(missing))

    st.divider()

    view = st.selectbox("檢視", ["完全無回報", "部分回報（可能漏跳）", "完整結束"])

    if view == "完全無回報":
        rows = [{
            "時間": sig.signal_time.strftime("%m/%d %H:%M") if sig.signal_time else "",
            "來源": sig.source, "商品": sig.symbol,
            "方向": sig.side.value.upper() if sig.side else "",
            "進場": sig.entry, "止損": sig.sl,
            "TP1": sig.tp1 or "", "Key": sig.signal_key or "",
        } for sig in missing[:100]]
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=500)
        else:
            st.caption("全部都有回報")

    elif view == "部分回報（可能漏跳）":
        rows = []
        for sig, tps in partial[:100]:
            max_tp = max(int(t[-1]) for t in tps) if tps else 0
            rows.append({
                "時間": sig.signal_time.strftime("%m/%d %H:%M") if sig.signal_time else "",
                "來源": sig.source, "商品": sig.symbol,
                "方向": sig.side.value.upper() if sig.side else "",
                "進場": sig.entry, "最高TP": f"TP{max_tp}",
                "缺少": f"TP{max_tp+1}~TP4 或 SL", "Key": sig.signal_key or "",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=500)
        else:
            st.caption("無部分回報的信號")

    else:
        rows = []
        for sig in complete[:100]:
            ups = update_by_sig.get(sig.id, [])
            tp_levels = set()
            has_sl = False
            for u in ups:
                if u.update_type and u.update_type.value == "tp_hit" and u.update_value:
                    tp_levels.add(u.update_value)
                elif u.update_type and u.update_type.value in ("close_now", "cancel"):
                    has_sl = True
            exit_type = "SL" if has_sl else f"TP{max(int(t[-1]) for t in tp_levels)}"
            rows.append({
                "時間": sig.signal_time.strftime("%m/%d %H:%M") if sig.signal_time else "",
                "商品": sig.symbol,
                "方向": sig.side.value.upper() if sig.side else "",
                "進場": sig.entry, "結束": exit_type, "Key": sig.signal_key or "",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=500)
        else:
            st.caption("無完整結束的信號")


def tab_signals(results, signals):
    """信號管線：訊息流 + 漏跳偵測"""
    tab_monitor(results, signals)
    with st.expander("漏跳偵測"):
        tab_missed(results, signals)

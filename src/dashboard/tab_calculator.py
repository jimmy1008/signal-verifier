"""Tab: 交易計算機 — 獨立記錄交易，計算勝率/RR/累積R"""

from __future__ import annotations
import json
import streamlit as st
from pathlib import Path
from datetime import datetime

from src.dashboard.helpers import GREEN, RED, BLUE, GRAY, get_project_root

_SAVE_PATH = Path(get_project_root()) / "db" / "calculator_trades.json"


def _load_trades() -> list[dict]:
    try:
        if _SAVE_PATH.exists():
            return json.loads(_SAVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_trades(trades: list[dict]):
    _SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SAVE_PATH.write_text(json.dumps(trades, ensure_ascii=False, indent=2), encoding="utf-8")


def tab_calculator():
    trades = _load_trades()

    # ── 輸入區 ──
    st.markdown(
        '<div style="border-left:3px solid #30363d;padding-left:12px;margin-bottom:12px">'
        '<span style="font-size:0.65rem;color:#888;text-transform:uppercase;letter-spacing:1.5px;font-weight:600">'
        '新增交易</span></div>', unsafe_allow_html=True)

    with st.form("calc_form", clear_on_submit=False):
        c1, c2, c3, c4, c5 = st.columns([2, 1.5, 1.5, 1.5, 1.5])
        symbol = c1.text_input("幣種", placeholder="ETHUSDT", key="calc_sym")
        side = c2.selectbox("方向", ["LONG", "SHORT"], key="calc_side")
        entry = c3.number_input("進場價", value=0.0, format="%.6f", key="calc_entry")
        sl = c4.number_input("止損價", value=0.0, format="%.6f", key="calc_sl")
        tp = c5.number_input("止盈價", value=0.0, format="%.6f", key="calc_tp")

        col_tp, col_sl, col_be = st.columns(3)
        btn_tp = col_tp.form_submit_button("TP (止盈)", use_container_width=True, type="primary")
        btn_sl = col_sl.form_submit_button("SL (止損)", use_container_width=True)
        btn_be = col_be.form_submit_button("BE (保本)", use_container_width=True)

    if btn_tp and symbol and entry > 0:
        risk = abs(entry - sl) if sl else 1
        reward = abs(tp - entry) if tp else 0
        rr = reward / risk if risk > 0 else 0
        trades.append({
            "time": datetime.now().strftime("%m/%d %H:%M"),
            "symbol": symbol.upper(), "side": side,
            "entry": entry, "sl": sl, "tp": tp,
            "result": "TP", "pnl_r": round(rr, 4),
        })
        _save_trades(trades)
        st.rerun()
    elif btn_sl and symbol and entry > 0:
        trades.append({
            "time": datetime.now().strftime("%m/%d %H:%M"),
            "symbol": symbol.upper(), "side": side,
            "entry": entry, "sl": sl, "tp": tp,
            "result": "SL", "pnl_r": -1.0,
        })
        _save_trades(trades)
        st.rerun()
    elif btn_be and symbol and entry > 0:
        trades.append({
            "time": datetime.now().strftime("%m/%d %H:%M"),
            "symbol": symbol.upper(), "side": side,
            "entry": entry, "sl": sl, "tp": tp,
            "result": "BE", "pnl_r": 0.0,
        })
        _save_trades(trades)
        st.rerun()

    st.divider()

    # ── 統計 ──
    if trades:
        tp_trades = [t for t in trades if t["result"] == "TP"]
        sl_trades = [t for t in trades if t["result"] == "SL"]
        be_trades = [t for t in trades if t["result"] == "BE"]
        total = len(trades)
        wl = len(tp_trades) + len(sl_trades)
        wr = len(tp_trades) / wl * 100 if wl > 0 else 0
        cum_r = sum(t["pnl_r"] for t in trades)
        avg_win = sum(t["pnl_r"] for t in tp_trades) / len(tp_trades) if tp_trades else 0
        avg_loss = sum(t["pnl_r"] for t in sl_trades) / len(sl_trades) if sl_trades else 0
        rr = abs(avg_win / avg_loss) if avg_loss else 0
        ev = (len(tp_trades) / total * avg_win + len(sl_trades) / total * avg_loss) if total else 0

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("總筆數", f"{total}")
        m2.metric("勝率", f"{wr:.1f}%")
        m3.metric("平均 RR", f"{rr:.2f}")
        cum_color = "normal" if cum_r >= 0 else "inverse"
        m4.metric("累積 R", f"{cum_r:+.2f}")
        m5.metric("期望值", f"{ev:+.4f}R")

        # ── 交易紀錄表 ──
        st.markdown(
            '<div style="border-left:3px solid #30363d;padding-left:12px;margin:12px 0 8px 0">'
            '<span style="font-size:0.65rem;color:#888;text-transform:uppercase;letter-spacing:1.5px;font-weight:600">'
            '交易紀錄</span></div>', unsafe_allow_html=True)

        for idx in range(len(trades) - 1, -1, -1):
            t = trades[idx]
            r = t["result"]
            if r == "TP":
                r_html = f'<span style="color:{GREEN};font-weight:700">TP</span>'
                pnl_html = f'<span style="color:{GREEN}">+{t["pnl_r"]:.2f}R</span>'
            elif r == "SL":
                r_html = f'<span style="color:{RED};font-weight:700">SL</span>'
                pnl_html = f'<span style="color:{RED}">{t["pnl_r"]:.2f}R</span>'
            else:
                r_html = f'<span style="color:{BLUE};font-weight:700">BE</span>'
                pnl_html = f'<span style="color:#555">0.00R</span>'

            row_cols = st.columns([1.2, 1.5, 0.8, 1.5, 0.6, 0.8, 0.5])
            row_cols[0].markdown(f'<span style="font-family:JetBrains Mono,monospace;font-size:0.72rem;color:#555">{t["time"]}</span>', unsafe_allow_html=True)
            row_cols[1].markdown(f'<span style="font-family:JetBrains Mono,monospace;font-size:0.72rem;color:#ccc">{t["symbol"]}</span>', unsafe_allow_html=True)
            row_cols[2].markdown(f'<span style="font-family:JetBrains Mono,monospace;font-size:0.72rem;color:#888">{t["side"]}</span>', unsafe_allow_html=True)
            row_cols[3].markdown(f'<span style="font-family:JetBrains Mono,monospace;font-size:0.72rem;color:#888">{t["entry"]}</span>', unsafe_allow_html=True)
            row_cols[4].markdown(r_html, unsafe_allow_html=True)
            row_cols[5].markdown(pnl_html, unsafe_allow_html=True)
            if row_cols[6].button("x", key=f"del_{idx}", help="刪除此筆"):
                trades.pop(idx)
                _save_trades(trades)
                st.rerun()

        # Clear button
        st.divider()
        if st.button("清空所有紀錄", type="secondary"):
            _save_trades([])
            st.rerun()
    else:
        st.caption("尚無交易紀錄")

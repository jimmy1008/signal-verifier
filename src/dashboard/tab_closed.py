"""Tab: 已平倉 — 平倉紀錄 + 執行日誌"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import streamlit as st

from src.dashboard.helpers import (
    GREEN, RED, BLUE, GRAY,
    get_bingx_data, build_closed_positions, get_project_root,
)

_log = logging.getLogger(__name__)


def tab_closed(results, signals):
    """已平倉位 + 執行日誌（從 tab_exec 抽出）"""
    data = get_bingx_data(fetch_trades=True)
    h1_account, h1_positions, h1_trades = data[0], data[1], data[2]
    h4_account, h4_positions, h4_trades = data[3], data[4], data[5]

    if not h1_account:
        st.warning("BingX 未設定 API Key")
        return

    all_closed = build_closed_positions((h1_trades or []) + (h4_trades or []))
    total_pnl = sum(c["pnl"] for c in all_closed)
    total_fee = sum(c["fee"] for c in all_closed)
    tp_count = sum(1 for c in all_closed if c["exit"] == "TP")
    sl_count = sum(1 for c in all_closed if c["exit"] == "SL")
    be_count = sum(1 for c in all_closed if c["exit"] == "BE")
    wl = tp_count + sl_count
    wr = tp_count / wl * 100 if wl > 0 else 0

    # 頂部指標
    pnl_color = GREEN if total_pnl > 0 else (RED if total_pnl < 0 else "#888")
    st.markdown(
        f'<div style="display:flex;gap:24px;align-items:baseline;margin-bottom:8px;font-family:JetBrains Mono,monospace">'
        f'<span style="font-size:0.9rem;color:#888">已平倉 <b style="color:#ccc">{len(all_closed)}</b></span>'
        f'<span style="font-size:0.9rem;color:#888">淨盈虧 <b style="color:{pnl_color}">${total_pnl:+.4f}</b></span>'
        f'<span style="font-size:0.9rem;color:#888">勝率 <b style="color:#ccc">{wr:.1f}%</b> ({tp_count}W/{sl_count}L/{be_count}BE)</span>'
        f'<span style="font-size:0.9rem;color:#888">手續費 <b style="color:#555">${total_fee:.4f}</b></span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # 歷史倉位表
    col_hist, col_log = st.columns([6, 4])

    def _render_closed_table_standalone(trades):
        closed = build_closed_positions(trades)
        if not closed:
            st.caption("尚無平倉紀錄")
            return
        html_rows = ""
        for c in closed[:30]:
            ex = c["exit"]
            if ex == "TP":
                exit_html = f'<span style="color:{GREEN};font-weight:700">TP</span>'
            elif ex == "SL":
                exit_html = f'<span style="color:{RED};font-weight:700">SL</span>'
            elif ex == "BE":
                exit_html = f'<span style="color:{BLUE};font-weight:700">BE</span>'
            else:
                exit_html = f'<span style="color:{GRAY}">{ex}</span>'
            pnl = c["pnl"]
            if pnl > 0.001:
                pnl_html = f'<span class="pnl-pos">${pnl:+.4f}</span>'
            elif pnl < -0.001:
                pnl_html = f'<span class="pnl-neg">${pnl:+.4f}</span>'
            else:
                pnl_html = f'<span class="pnl-zero">${pnl:+.4f}</span>'
            fee_html = f'<span style="color:#555">${c.get("fee", 0):.4f}</span>'
            html_rows += (
                f'<tr style="border-bottom:1px solid #1a1f2b">'
                f'<td style="padding:6px">{c["time"]}</td>'
                f'<td style="padding:6px">{c["sym"]}</td>'
                f'<td style="padding:6px">{c["dir"]}</td>'
                f'<td style="padding:6px;text-align:center">{exit_html}</td>'
                f'<td style="padding:6px;text-align:right">{pnl_html}</td>'
                f'<td style="padding:6px;text-align:right">{fee_html}</td>'
                f'</tr>'
            )
        st.markdown(f"""
        <div style="max-height:calc(100vh - 220px);overflow-y:auto">
        <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono','Consolas',monospace;font-size:0.78rem">
        <thead><tr style="border-bottom:1px solid #333;color:#666;font-size:0.6rem;text-transform:uppercase;letter-spacing:0.8px">
            <th style="padding:6px;text-align:left">平倉時間</th>
            <th style="padding:6px;text-align:left">幣種</th>
            <th style="padding:6px;text-align:left">方向</th>
            <th style="padding:6px;text-align:center">出場</th>
            <th style="padding:6px;text-align:right">盈虧</th>
            <th style="padding:6px;text-align:right">手續費</th>
        </tr></thead>
        <tbody>{html_rows}</tbody>
        </table></div>
        """, unsafe_allow_html=True)

    with col_hist:
        hist_h1, hist_h4 = st.tabs(["H1 主帳戶", "H4 子帳戶"])
        with hist_h1:
            _render_closed_table_standalone(h1_trades or [])
        with hist_h4:
            _render_closed_table_standalone(h4_trades or [])

    with col_log:
        st.markdown("**執行日誌**")
        root = get_project_root()
        log_path = Path(f"{root}/auto_trade.log")
        if log_path.exists():
            try:
                with open(str(log_path), "r", encoding="utf-8", errors="replace") as f:
                    log_lines = f.readlines()
                filtered = [l for l in log_lines if "telethon" not in l.lower() and "Deprecation" not in l and l.strip()]
                log_html = ""
                for line in filtered[-20:]:
                    line = line.rstrip()
                    line_esc = line.replace("<", "&lt;").replace(">", "&gt;")
                    line_esc = line_esc.replace("TP_HIT", f'<span class="log-tp">TP_HIT</span>')
                    line_esc = line_esc.replace("tp_hit", f'<span class="log-tp">tp_hit</span>')
                    line_esc = line_esc.replace("CLOSE_NOW", f'<span class="log-sl">CLOSE_NOW</span>')
                    line_esc = line_esc.replace("sl_hit", f'<span class="log-sl">sl_hit</span>')
                    line_esc = line_esc.replace("保本", f'<span style="color:{BLUE}">保本</span>')
                    if "[INFO]" in line:
                        parts = line_esc.split("[INFO]", 1)
                        log_html += f'<span class="log-time">{parts[0]}</span><span class="log-info">[INFO]</span><span class="log-msg">{parts[1] if len(parts) > 1 else ""}</span><br>'
                    elif "[ERROR]" in line:
                        log_html += f'<span style="color:{RED}">{line_esc}</span><br>'
                    elif "[WARNING]" in line:
                        log_html += f'<span class="log-warn">{line_esc}</span><br>'
                    else:
                        log_html += f'<span class="log-msg">{line_esc}</span><br>'
                from datetime import datetime as _dtl
                last_update = _dtl.now().strftime("%H:%M:%S")
                st.markdown(
                    f'<div class="log-box">{log_html}</div>'
                    f'<div style="text-align:right;font-size:0.55rem;color:#444;margin-top:2px">更新 {last_update}</div>',
                    unsafe_allow_html=True)
            except Exception:
                st.caption("無法讀取")
        else:
            st.caption("尚無 Log")

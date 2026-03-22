"""Tab: 執行總覽 — 帳戶、持倉、資金曲線、收益概覽"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.dashboard.helpers import (
    TRADE_START_TS, BG, CARD, BORDER, GREEN, RED, YELLOW, BLUE, GRAY, TEXT,
    get_bingx_data, build_closed_positions, get_project_root, playout,
)

_log = logging.getLogger(__name__)


def _render_equity_curve(account, recent_trades):
    """根據 BingX 成交紀錄建立資金曲線"""
    if not recent_trades:
        st.caption("尚無成交紀錄，無法生成資金曲線")
        return

    from datetime import datetime as _dt

    # 用 build_closed_positions 算已平倉盈虧（精確）
    closed_list = build_closed_positions(recent_trades)
    if not closed_list:
        st.caption("尚無已平倉紀錄")
        return

    initial_capital = account.get("initial_capital", 100.0)

    # 建立資金曲線（按平倉時間排序）
    sorted_closed = sorted(closed_list, key=lambda c: c["time"])
    times = [_dt.strptime(f"2026/{sorted_closed[0]['time']}", "%Y/%m/%d %H:%M")]
    equity = [initial_capital]
    cum = initial_capital

    for c in sorted_closed:
        cum += c["pnl"]
        try:
            ts = _dt.strptime(f"2026/{c['time']}", "%Y/%m/%d %H:%M")
        except Exception:
            ts = times[-1]
        times.append(ts)
        equity.append(cum)

    # 手續費從成交紀錄統計
    trade_points = []
    for t in sorted(recent_trades, key=lambda x: x.get("timestamp", 0)):
        try:
            fee_obj = t.get("fee") or {}
            fee = abs(float(fee_obj.get("cost", 0))) if isinstance(fee_obj, dict) else 0
            trade_points.append({"fee": fee})
        except Exception:
            pass

    eq_arr = np.array(equity)
    peak = np.maximum.accumulate(eq_arr)
    dd_pct = (eq_arr - peak) / np.where(peak > 0, peak, 1) * 100

    # 資金曲線（單圖）
    fig = go.Figure()

    is_profit = eq_arr[-1] >= initial_capital
    line_color = GREEN if is_profit else RED
    fill_color = "rgba(0,200,5,0.06)" if is_profit else "rgba(255,75,75,0.06)"

    fig.add_trace(go.Scatter(
        x=times, y=equity, mode="lines", name="資金",
        line=dict(color=line_color, width=2),
        fill="tozeroy", fillcolor=fill_color,
    ))

    # 初始資金基線
    fig.add_hline(
        y=initial_capital, line_dash="dot", line_color=GRAY,
        annotation_text=f"初始 ${initial_capital:,.2f}",
        annotation_font_color=GRAY, annotation_font_size=10,
    )

    # 峰值線
    fig.add_trace(go.Scatter(
        x=times, y=peak.tolist(), mode="lines", name="峰值",
        line=dict(color=BLUE, width=1, dash="dot"), opacity=0.4,
    ))

    fig.update_layout(
        **playout("", 250),
        showlegend=False,
    )
    fig.update_yaxes(
        title_text="$", tickformat=",.2f",
        gridcolor=BORDER, zerolinecolor=BORDER,
    )

    st.plotly_chart(fig, use_container_width=True)

    # 曲線下方指標
    max_dd = float(np.min(dd_pct))
    total_return_pct = (eq_arr[-1] - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0
    total_fees = sum(p["fee"] for p in trade_points)

    # 勝率（用 build_closed_positions，BE 不計入勝敗）
    closed_list = build_closed_positions(recent_trades)
    wins = sum(1 for c in closed_list if c["exit"] == "TP")
    losses = sum(1 for c in closed_list if c["exit"] == "SL")
    be_count = sum(1 for c in closed_list if c["exit"] == "BE")
    total_closed = wins + losses  # BE 不影響勝率
    win_rate = wins / total_closed * 100 if total_closed > 0 else 0

    # 平均 RR（TP 平均盈利 / SL 平均虧損）
    tp_pnls = [c["pnl"] for c in closed_list if c["exit"] == "TP"]
    sl_pnls = [c["pnl"] for c in closed_list if c["exit"] == "SL"]
    avg_win = sum(tp_pnls) / len(tp_pnls) if tp_pnls else 0
    avg_loss = sum(sl_pnls) / len(sl_pnls) if sl_pnls else 0
    avg_rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    kc1, kc2, kc3, kc4, kc5 = st.columns(5)
    kc1.metric("累計報酬", f"{total_return_pct:+.2f}%")
    kc2.metric("勝率", f"{win_rate:.1f}% ({wins}W/{losses}L/{be_count}BE)")
    kc3.metric("平均 RR", f"{avg_rr:.2f}")
    kc4.metric("最大回撤", f"{max_dd:.2f}%")
    kc5.metric("累計手續費", f"${total_fees:,.4f}")


def _render_account_section(label, account, positions, ex):
    """渲染單一帳戶：緊湊 header + 進度條 + tabs"""
    acct_color = BLUE if "H1" in label else YELLOW
    upnl = account["unrealized_pnl"]
    upnl_class = "pnl-pos" if upnl > 0 else ("pnl-neg" if upnl < 0 else "pnl-zero")
    pos_count = len(positions) if positions else 0

    # 保證金比例
    total = account["balance"]
    used = account["used"]
    ratio = used / total if total > 0 else 0
    bar_color = BLUE if ratio < 0.6 else (YELLOW if ratio < 0.8 else RED)

    # 緊湊 header：標題 + 指標一行
    st.markdown(
        f'<div style="border-top:3px solid {acct_color};background:#161b22;border-radius:8px;padding:10px 14px;margin-bottom:6px">'
        f'<div style="display:flex;align-items:center;justify-content:space-between">'
        f'<span style="font-weight:800;font-size:0.95rem;color:white">{label}</span>'
        f'<div style="display:flex;gap:16px;font-family:JetBrains Mono,monospace;font-size:0.75rem">'
        f'<span style="color:#888">餘額 <span style="color:#e6e6e6">${account["balance"]:.2f}</span></span>'
        f'<span style="color:#888">淨值 <span style="color:white;font-weight:700">${account["equity"]:.2f}</span></span>'
        f'<span style="color:#888">持倉 <span style="color:#e6e6e6">{pos_count}</span></span>'
        f'<span style="color:#888">浮盈 <span class="{upnl_class}">${upnl:+.2f}</span></span>'
        f'</div></div>'
        f'<div class="margin-bar" style="margin-top:8px"><div class="margin-fill" style="width:{ratio*100:.0f}%;background:{bar_color}"></div></div>'
        f'<div style="display:flex;justify-content:space-between;font-size:0.55rem;color:#555;margin-top:2px">'
        f'<span>可用 ${account["available"]:.2f}</span><span>佔用 ${used:.2f} ({ratio:.0%})</span></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    tab_pos, tab_orders = st.tabs(["持倉", "掛單"])
    with tab_pos:
        if positions:
            # 自訂 HTML 表格，支持方向標籤和盈虧顏色
            rows_html = ""
            for p in positions:
                d = p.get("方向", "")
                tag = f'<span class="tag-long">LONG</span>' if d == "LONG" else f'<span class="tag-short">SHORT</span>'
                pnl_str = p.get("浮盈", "$0")
                pnl_val = float(pnl_str.replace("$", "").replace("+", ""))
                pnl_c = "pnl-pos" if pnl_val > 0 else ("pnl-neg" if pnl_val < 0 else "pnl-zero")
                pct_str = p.get("盈虧%", "0%")
                pct_val = float(pct_str.replace("%", "").replace("+", ""))
                pct_c = "pnl-pos" if pct_val > 0 else ("pnl-neg" if pct_val < 0 else "pnl-zero")

                sym_short = str(p.get("商品", "")).replace("/USDT:USDT", "")
                rows_html += (
                    f'<tr style="border-bottom:1px solid #1a1f2b">'
                    f'<td style="padding:5px 6px">{sym_short}</td>'
                    f'<td style="padding:5px 6px">{tag}</td>'
                    f'<td style="padding:5px 6px;text-align:right">{p.get("保證金","")}</td>'
                    f'<td style="padding:5px 6px;text-align:right">{p.get("進場價","")}</td>'
                    f'<td style="padding:5px 6px;text-align:right">{p.get("標記價","")}</td>'
                    f'<td style="padding:5px 6px;text-align:right"><span class="{pnl_c}">{pnl_str}</span></td>'
                    f'<td style="padding:5px 6px;text-align:right"><span class="{pct_c}">{pct_str}</span></td>'
                    f'<td style="padding:5px 6px;text-align:right;color:#555;font-size:0.7rem">{p.get("槓桿","")}</td>'
                    f'</tr>'
                )
            st.markdown(
                f'<table style="width:100%;border-collapse:collapse;font-family:JetBrains Mono,monospace;font-size:0.75rem">'
                f'<thead><tr style="border-bottom:1px solid #333;color:#555;font-size:0.6rem;text-transform:uppercase;letter-spacing:0.5px">'
                f'<th style="padding:5px 6px;text-align:left">商品</th>'
                f'<th style="padding:5px 6px;text-align:left">方向</th>'
                f'<th style="padding:5px 6px;text-align:right">保證金</th>'
                f'<th style="padding:5px 6px;text-align:right">進場</th>'
                f'<th style="padding:5px 6px;text-align:right">標記</th>'
                f'<th style="padding:5px 6px;text-align:right">浮盈</th>'
                f'<th style="padding:5px 6px;text-align:right">盈虧%</th>'
                f'<th style="padding:5px 6px;text-align:right">槓桿</th>'
                f'</tr></thead><tbody>{rows_html}</tbody></table>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("目前無持倉")

    with tab_orders:
        if ex:
            try:
                all_orders = []
                syms_to_check = set()
                if positions:
                    for p in positions:
                        syms_to_check.add(p["商品"])
                syms_to_check.update(["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"])
                for sym in list(syms_to_check)[:10]:
                    try:
                        orders = ex.fetch_open_orders(sym)
                        all_orders.extend(orders)
                    except Exception as _e:
                        _log.debug(f"靜默異常: {_e}")
                        pass
                if all_orders:
                    # 建立持倉查找表（symbol+side → entry_price, contracts）
                    pos_lookup = {}
                    if positions:
                        for p in positions:
                            key = (p["商品"], p["方向"])
                            try:
                                pos_lookup[key] = {
                                    "entry": float(p["進場價"]),
                                    "contracts": float(p.get("保證金", "0").replace("$", "")),
                                }
                            except (ValueError, TypeError):
                                pass

                    order_rows = []
                    for o in all_orders:
                        info = o.get("info", {})
                        stop_p = o.get("stopPrice") or info.get("stopPrice") or o.get("price")
                        try:
                            stop_val = float(stop_p) if stop_p else 0
                            stop_str = f"{stop_val:.4f}" if stop_val else "-"
                        except (ValueError, TypeError):
                            stop_val = 0
                            stop_str = "-"

                        sym_full = str(o.get("symbol", ""))
                        sym = sym_full.replace("/USDT:USDT", "")
                        order_type = str(info.get("type", "")).upper()
                        order_side = str(o.get("side", "")).upper()
                        pos_side = info.get("positionSide", "")
                        amount = float(o.get("amount", 0) or 0)

                        # 預估平倉盈虧：(觸發價 - 進場價) × 數量 × 方向
                        est_pnl_str = "-"
                        if stop_val > 0 and amount > 0:
                            # 找對應持倉的進場價
                            pos_dir = "LONG" if pos_side == "LONG" else "SHORT"
                            for p in (positions or []):
                                if sym in str(p.get("商品", "")) and p.get("方向") == pos_dir:
                                    try:
                                        entry = float(p["進場價"])
                                        if pos_dir == "LONG":
                                            pnl = (stop_val - entry) * amount
                                        else:
                                            pnl = (entry - stop_val) * amount
                                        est_pnl_str = f"${pnl:+.4f}"
                                    except (ValueError, TypeError):
                                        pass
                                    break

                        order_rows.append({
                            "商品": sym,
                            "類型": order_type.replace("_", " "),
                            "方向": order_side,
                            "觸發價": stop_str,
                            "預估盈虧": est_pnl_str,
                        })
                    st.dataframe(pd.DataFrame(order_rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("無掛單")
            except Exception as e:
                st.caption(f"查詢失敗: {e}")


def _check_timeframe_mismatch(h1_positions, h4_positions):
    """檢查新 bot 開的倉位是否路由到錯誤帳戶"""
    warnings = []

    root = get_project_root()
    log_path = Path(f"{root}/auto_trade.log")
    if not log_path.exists():
        return warnings

    import re
    # 匹配: [新信號] SOLUSDT.P long @ 89.95 (4h → H1主帳戶)
    pattern = re.compile(r"\[新信號\] (\S+) \S+ @ [\d.]+ \((\w+) → (H\d\S+)\)")

    try:
        with open(str(log_path), "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in lines:
            m = pattern.search(line)
            if not m:
                continue
            symbol, tf, target = m.group(1), m.group(2), m.group(3)

            # 4h 信號應該去 H4，1h 信號應該去 H1
            if "4" in tf and "H1" in target:
                warnings.append(f"4H 信號 {symbol} 被路由到 H1 主帳戶")
            elif "4" not in tf and "H4" in target:
                warnings.append(f"1H 信號 {symbol} 被路由到 H4 子帳戶")
    except Exception as _e:
        _log.debug(f"靜默異常: {_e}")
        pass

    return warnings


@st.dialog("收益概覽", width="large")
def _show_profit_card():
    """極簡分享卡：收益率 + 資金曲線 + 關鍵指標"""
    data = get_bingx_data(fetch_trades=True)
    h1_account, h1_positions, h1_trades = data[0], data[1], data[2]
    h4_account, h4_positions, h4_trades = data[3], data[4], data[5]

    if not h1_account:
        st.warning("無法取得帳戶資料")
        return

    from datetime import datetime as _dt

    total_balance = h1_account["balance"] + (h4_account["balance"] if h4_account else 0)
    total_equity = h1_account["equity"] + (h4_account["equity"] if h4_account else 0)
    total_upnl = h1_account["unrealized_pnl"] + (h4_account["unrealized_pnl"] if h4_account else 0)
    total_positions = (len(h1_positions) if h1_positions else 0) + (len(h4_positions) if h4_positions else 0)
    pnl_pct = total_upnl / total_balance * 100 if total_balance > 0 else 0
    is_profit = total_upnl >= 0
    pnl_color = GREEN if is_profit else RED
    pnl_sign = "+" if pnl_pct >= 0 else ""

    # ── 大收益率 ──
    st.markdown(
        f'<div style="text-align:center;padding:20px 0 8px 0">'
        f'<div style="font-size:3rem;font-weight:900;font-family:JetBrains Mono,monospace;color:{pnl_color};line-height:1">'
        f'{pnl_sign}{pnl_pct:.2f}%</div>'
        f'<div style="font-size:0.9rem;color:{pnl_color};font-family:JetBrains Mono,monospace;margin-top:4px">'
        f'${total_upnl:+.2f}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── 資金曲線 ──
    all_trades = (h1_trades or []) + (h4_trades or [])
    all_trades.sort(key=lambda x: x.get("timestamp", 0))

    trade_points = []
    for t in all_trades:
        try:
            ts = _dt.fromtimestamp(t.get("timestamp", 0) / 1000)
            pnl = float(t.get("info", {}).get("realizedPnl", 0)) if t.get("info") else 0
            fee_obj = t.get("fee") or {}
            fee = abs(float(fee_obj.get("cost", 0))) if isinstance(fee_obj, dict) else 0
            trade_points.append({"time": ts, "pnl": pnl - fee, "fee": fee})
        except Exception as _e:
            _log.debug(f"靜默異常: {_e}")
            continue

    if trade_points:
        total_realized = sum(p["pnl"] for p in trade_points)
        initial_capital = total_balance - total_realized
        times = [trade_points[0]["time"]]
        equity_line = [initial_capital]
        cum = initial_capital
        for p in trade_points:
            cum += p["pnl"]
            times.append(p["time"])
            equity_line.append(cum)

        eq_arr = np.array(equity_line)
        peak = np.maximum.accumulate(eq_arr)
        max_dd = float(np.min((eq_arr - peak) / np.where(peak > 0, peak, 1) * 100))
        total_return_pct = (eq_arr[-1] - initial_capital) / initial_capital * 100 if initial_capital > 0 else 0

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=times, y=equity_line, mode="lines",
            line=dict(color=pnl_color, width=2.5),
            fill="tozeroy",
            fillcolor=f"rgba({'0,200,5' if is_profit else '255,75,75'},0.06)",
        ))
        fig.add_hline(y=initial_capital, line_dash="dot", line_color="#333", opacity=0.5)
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor=CARD, plot_bgcolor=CARD,
            margin=dict(l=45, r=10, t=8, b=25),
            height=280,
            font=dict(color="#555", size=10, family="JetBrains Mono"),
            xaxis=dict(gridcolor="#1e2530", zerolinecolor="#1e2530", showgrid=False),
            yaxis=dict(gridcolor="#1e2530", zerolinecolor="#1e2530", tickformat=",.1f", showgrid=True),
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)

        # ── 底部一行指標 ──
        st.markdown(
            f'<div style="display:flex;justify-content:center;gap:32px;font-family:JetBrains Mono,monospace;'
            f'font-size:0.75rem;color:#888;padding:4px 0 8px 0">'
            f'<span>淨值 <span style="color:white;font-weight:700">${total_equity:.2f}</span></span>'
            f'<span>報酬 <span style="color:{pnl_color};font-weight:700">{total_return_pct:+.2f}%</span></span>'
            f'<span>回撤 <span style="color:{RED}">{max_dd:.2f}%</span></span>'
            f'<span>持倉 <span style="color:#ccc">{total_positions}</span></span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("尚無成交紀錄")

    # ── 水印 ──
    st.markdown(
        f'<div style="text-align:center;font-size:0.55rem;color:#252a33;margin-top:4px">'
        f'Signal Verifier | {_dt.now().strftime("%Y/%m/%d %H:%M")}</div>',
        unsafe_allow_html=True,
    )


def tab_exec(results, signals):
    # 曲線篩選（右上角）
    _, col_select = st.columns([8.5, 1.5])
    with col_select:
        curve_view = st.selectbox("曲線", ["全部", "H1", "H4"],
                                  label_visibility="collapsed", key="curve_view")

    # 用 fragment 實現局部自動刷新（不影響其他操作）
    @st.fragment(run_every=60)
    def _live_data():
        data = get_bingx_data(fetch_trades=True)
        h1_account, h1_positions, h1_trades = data[0], data[1], data[2]
        h4_account, h4_positions, h4_trades = data[3], data[4], data[5]
        h1_ex = data[6]

        if h1_account is None:
            st.warning("BingX 未設定 API Key"); return
        if isinstance(h1_account, dict) and "error" in h1_account:
            st.error(f"連線失敗: {h1_account['error']}"); return

        # ── 合併指標 ──
        total_balance = h1_account["balance"] + (h4_account["balance"] if h4_account else 0)
        total_equity = h1_account["equity"] + (h4_account["equity"] if h4_account else 0)
        total_upnl = h1_account["unrealized_pnl"] + (h4_account["unrealized_pnl"] if h4_account else 0)
        total_positions = (len(h1_positions) if h1_positions else 0) + (len(h4_positions) if h4_positions else 0)
        upnl_class = "pnl-pos" if total_upnl > 0 else ("pnl-neg" if total_upnl < 0 else "pnl-zero")
        pnl_border = GREEN if total_upnl > 0 else (RED if total_upnl < 0 else BORDER)

        # 統計 TP/SL/BE — 合併全量數據一次計算
        _all_closed = build_closed_positions((h1_trades or []) + (h4_trades or []))
        _tp_count = sum(1 for c in _all_closed if c["exit"] == "TP")
        _sl_count = sum(1 for c in _all_closed if c["exit"] == "SL")
        _be_count = sum(1 for c in _all_closed if c["exit"] == "BE")

        st.markdown(
            f'<div style="display:flex;gap:12px;margin-bottom:8px">'
            f'<div style="flex:1;background:#1e222d;border:1px solid #30363d;border-radius:10px;padding:12px 16px">'
            f'<div style="font-size:0.55rem;color:#666;text-transform:uppercase;letter-spacing:1px">總餘額</div>'
            f'<div style="font-size:1.1rem;font-family:JetBrains Mono,monospace;color:#e6e6e6;margin-top:4px">${total_balance:.2f}</div></div>'
            f'<div style="flex:1;background:#1e222d;border:1px solid #30363d;border-radius:10px;padding:12px 16px">'
            f'<div style="font-size:0.55rem;color:#666;text-transform:uppercase;letter-spacing:1px">總淨值</div>'
            f'<div style="font-size:1.3rem;font-family:JetBrains Mono,monospace;color:white;font-weight:800;margin-top:4px">${total_equity:.2f}</div></div>'
            f'<div style="flex:0.6;background:#1e222d;border:1px solid #30363d;border-radius:10px;padding:12px 16px">'
            f'<div style="font-size:0.55rem;color:#666;text-transform:uppercase;letter-spacing:1px">總持倉</div>'
            f'<div style="font-size:1.1rem;font-family:JetBrains Mono,monospace;color:#e6e6e6;margin-top:4px">{total_positions}</div></div>'
            # TP/SL/BE 統計
            f'<div style="flex:1;background:#1e222d;border:1px solid #30363d;border-radius:10px;padding:12px 16px">'
            f'<div style="font-size:0.55rem;color:#666;text-transform:uppercase;letter-spacing:1px">TP / SL / BE</div>'
            f'<div style="font-size:1.1rem;font-family:JetBrains Mono,monospace;margin-top:4px">'
            f'<span style="color:{GREEN}">{_tp_count}</span>'
            f'<span style="color:#444"> / </span>'
            f'<span style="color:{RED}">{_sl_count}</span>'
            f'<span style="color:#444"> / </span>'
            f'<span style="color:{BLUE}">{_be_count}</span>'
            f'</div></div>'
            # 總浮盈
            f'<div style="flex:1;background:{"rgba(0,200,5,0.05)" if total_upnl > 0 else ("rgba(255,75,75,0.05)" if total_upnl < 0 else "#1e222d")};'
            f'border:1px solid #30363d;border-top:2px solid {pnl_border};border-radius:10px;padding:12px 16px">'
            f'<div style="font-size:0.55rem;color:#666;text-transform:uppercase;letter-spacing:1px">總浮盈</div>'
            f'<div class="{upnl_class}" style="font-size:1.3rem;margin-top:4px">${total_upnl:+.2f}</div>'
            f'</div>'
            f'</div>', unsafe_allow_html=True,
        )

        # ── 時間級別錯誤檢查 ──
        tf_warnings = _check_timeframe_mismatch(h1_positions or [], h4_positions or [])
        if tf_warnings:
            for w in tf_warnings:
                st.error(f"時間級別錯誤: {w}")

        st.divider()

        # ── 資金曲線 + 今日概況 ──
        all_trades = (h1_trades or []) + (h4_trades or [])
        all_trades.sort(key=lambda x: x.get("timestamp", 0))

        col_curve, col_today = st.columns([7, 3])

        with col_curve:
            cv = st.session_state.get("curve_view", "全部")
            if cv == "H1":
                curve_account = {**h1_account, "initial_capital": 100.0}
                curve_trades = h1_trades or []
            elif cv == "H4" and h4_account:
                curve_account = {**h4_account, "initial_capital": 100.0}
                curve_trades = h4_trades or []
            else:
                curve_account = {"balance": total_balance, "initial_capital": 200.0}
                curve_trades = all_trades
            if curve_trades:
                _render_equity_curve(curve_account, curve_trades)

        with col_today:
            st.markdown("**今日概況**")
            from datetime import datetime as _dt
            today = _dt.utcnow().strftime("%Y-%m-%d")
            today_count = 0
            total_fee = 0.0
            for t in all_trades:
                try:
                    ts = _dt.fromtimestamp(t.get("timestamp", 0) / 1000)
                    if ts.strftime("%Y-%m-%d") != today:
                        continue
                    fee_obj = t.get("fee") or {}
                    fee = float(fee_obj.get("cost", 0)) if isinstance(fee_obj, dict) else 0
                    total_fee += abs(fee)
                    today_count += 1
                except Exception as _e:
                    _log.debug(f"靜默異常: {_e}")
                    continue

            st.metric("成交筆數", today_count)
            st.metric("累計手續費", f"${total_fee:.4f}")
            st.metric("初始資金", "$200")

        st.divider()

        # ── 雙帳戶左右並排 ──
        if h4_account:
            col_h1, col_h4 = st.columns(2)
            with col_h1:
                _render_account_section("H1 主帳戶", h1_account, h1_positions, h1_ex)
            with col_h4:
                import ccxt as _ccxt
                from src.config import load_config as _lc
                _bcfg = _lc().get("bingx", {})
                _h4_ex = _ccxt.bingx({"apiKey": _bcfg["sub_api_key"], "secret": _bcfg["sub_api_secret"], "options": {"defaultType": "swap"}}) if _bcfg.get("sub_api_key") else None
                _render_account_section("H4 子帳戶", h4_account, h4_positions, _h4_ex)
        else:
            _render_account_section("H1 主帳戶", h1_account, h1_positions, h1_ex)

    _live_data()

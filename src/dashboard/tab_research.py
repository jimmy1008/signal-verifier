"""Tab: 研究室 — 回測 / Edge 分析 / 資本模擬 / 裁決"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from src.stats.metrics import compute_metrics, build_equity_curve, TradeResult
from src.evaluator.judge import evaluate_edge
from src.capital.simulator import run_simulation
from src.stats.time_analysis import analyze_by_session

from src.dashboard.helpers import (
    BG, BORDER, GREEN, RED, YELLOW, BLUE, GRAY, TEXT, playout,
)


def tab_backtest(results, signals):
    # 時間範圍篩選
    from datetime import datetime, timedelta
    col_filter, _ = st.columns([3, 7])
    with col_filter:
        range_opt = st.selectbox("時間範圍", ["全部", "最近 1 個月", "最近 3 個月", "最近 6 個月"],
                                 key="bt_range", label_visibility="collapsed")
    if range_opt != "全部":
        months = {"最近 1 個月": 30, "最近 3 個月": 90, "最近 6 個月": 180}[range_opt]
        cutoff = datetime.utcnow() - timedelta(days=months)
        results = [r for r in results if r.entry_time and r.entry_time >= cutoff]

    m = compute_metrics(results)

    c1, c2, c3, c4, c5 = st.columns(5)
    pf = abs(m.avg_win_r * m.win_count / (m.avg_loss_r * m.loss_count)) if m.loss_count and m.avg_loss_r else 0
    c1.metric("淨利潤", f"{m.total_r:+.1f}R")
    c2.metric("勝率", f"{m.win_rate:.1%}")
    c3.metric("利潤因子", f"{pf:.2f}")
    c4.metric("最大回撤", f"{m.max_drawdown_r:.1f}R")
    c5.metric("期望值", f"{m.expectancy:+.4f}R")

    eq = build_equity_curve(results)
    if not eq.empty:
        cum = eq["cumulative_r"].values
        peak = np.maximum.accumulate(cum)
        dd = cum - peak

        fig = make_subplots(rows=2, cols=1, row_heights=[0.75, 0.25], shared_xaxes=True, vertical_spacing=0.03)

        fig.add_trace(go.Scatter(
            x=eq["time"], y=cum, mode="lines", name="Equity",
            line=dict(color=BLUE, width=2), fill="tozeroy", fillcolor="rgba(41,98,255,0.08)",
        ), row=1, col=1)

        max_dd_idx = np.argmax(peak - cum)
        peak_idx = np.argmax(cum[:max_dd_idx + 1]) if max_dd_idx > 0 else 0
        fig.add_vrect(
            x0=eq["time"].iloc[peak_idx], x1=eq["time"].iloc[max_dd_idx],
            fillcolor="rgba(255,75,75,0.06)", line_width=0, row=1, col=1,
        )

        fig.add_trace(go.Scatter(
            x=eq["time"], y=dd, mode="lines", name="Drawdown",
            line=dict(color=RED, width=1), fill="tozeroy", fillcolor="rgba(255,75,75,0.12)",
        ), row=2, col=1)

        fig.update_layout(**playout("", 380))
        fig.update_yaxes(title_text="R", row=1, col=1)
        fig.update_yaxes(title_text="DD", row=2, col=1)
        st.plotly_chart(fig, use_container_width=True)

    tp_c = st.columns(6)
    tp_c[0].metric("TP1", f"{m.tp1_hit_rate:.0%}")
    tp_c[1].metric("TP2", f"{m.tp2_hit_rate:.0%}")
    tp_c[2].metric("TP3", f"{m.tp3_hit_rate:.0%}")
    tp_c[3].metric("TP4", f"{m.tp4_hit_rate:.0%}")
    tp_c[4].metric("TP1後SL", f"{m.tp1_hit_then_sl_rate:.0%}")
    tp_c[5].metric("保本", m.breakeven_count)

    with st.expander("交易明細"):
        rows = []
        for r in results:
            sig = signals.get(r.signal_id)
            if not sig:
                continue
            rows.append({
                "時間": r.entry_time.strftime("%m/%d %H:%M") if r.entry_time else "",
                "商品": sig.symbol,
                "方向": sig.side.value.upper(),
                "進場": sig.entry,
                "止損": sig.sl,
                "最高TP": f"TP{r.max_tp_hit}" if r.max_tp_hit else "-",
                "出場": r.exit_reason.value if r.exit_reason else "-",
                "R值": f"{r.pnl_r:+.2f}" if r.triggered else "-",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=350)


def tab_edge(results, signals):
    triggered = [r for r in results if r.triggered]
    m = compute_metrics(results)

    col_heat, col_delay = st.columns(2)

    with col_heat:
        hm = np.full((7, 24), np.nan)
        hc = np.zeros((7, 24))
        for r in triggered:
            sig = signals.get(r.signal_id)
            if not sig or not sig.signal_time:
                continue
            d, h = sig.signal_time.weekday(), sig.signal_time.hour
            hc[d][h] += 1
            if np.isnan(hm[d][h]):
                hm[d][h] = 0
            if r.pnl_r > 0:
                hm[d][h] += 1

        with np.errstate(divide="ignore", invalid="ignore"):
            rate = np.where(hc > 0, hm / hc * 100, np.nan)

        fig_h = go.Figure(go.Heatmap(
            z=rate,
            x=[f"{h:02d}" for h in range(24)],
            y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            colorscale=[[0, RED], [0.5, "#333"], [1, GREEN]],
            zmin=0, zmax=100, hoverongaps=False,
            text=np.where(hc > 0, hc.astype(int).astype(str), ""),
            texttemplate="%{text}", textfont=dict(size=8),
            colorbar=dict(title="Win%", len=0.6),
        ))
        fig_h.update_layout(**playout("勝率熱力圖 (小時 x 星期)", 300))
        st.plotly_chart(fig_h, use_container_width=True)

    with col_delay:
        delays = [0, 5, 10, 20, 30, 60]
        base = m.total_r
        cost_sec = 0.006
        ideal = [base] * len(delays)
        actual = [base - d * cost_sec * m.triggered_count for d in delays]

        fig_d = go.Figure()
        fig_d.add_trace(go.Scatter(x=delays, y=ideal, name="理想", line=dict(color=GRAY, dash="dash")))
        fig_d.add_trace(go.Scatter(
            x=delays, y=actual, name="實際",
            line=dict(color=GREEN, width=2), fill="tonexty", fillcolor="rgba(0,200,5,0.05)",
        ))
        fig_d.update_layout(**playout("延遲衰減 (秒 > R)", 300))
        fig_d.update_xaxes(title="延遲 (秒)")
        st.plotly_chart(fig_d, use_container_width=True)

    tr = analyze_by_session(results, signals)
    lm = {"asia": "亞洲 00-08", "europe": "歐洲 08-14", "us": "美盤 14-21", "cross": "交叉 21-00"}
    rows = []
    for n, s in tr.sessions.items():
        if s.metrics and s.trade_count > 0:
            rows.append({
                "時段": lm.get(n, n),
                "數量": s.trade_count,
                "勝率": f"{s.metrics.win_rate:.0%}",
                "期望值": f"{s.metrics.expectancy:+.4f}R",
                "累積R": f"{s.metrics.total_r:+.1f}",
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _fmt_money(v):
    """格式化金額"""
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:,.1f}B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:,.1f}M"
    if abs(v) >= 10_000:
        return f"${v / 1_000:,.1f}K"
    return f"${v:,.0f}"


def _sim_equity(triggered_r, cap_init, risk_pct, friction=0.001, cap_max=50_000_000):
    """模擬資金曲線（含滑價摩擦 + 容量上限），回傳 % 收益序列"""
    eq_pct = [0.0]
    c = float(cap_init)
    peak = c
    max_dd = 0.0
    for p in triggered_r:
        scale_friction = friction * (1 + max(0, c - cap_init) / cap_init)
        pnl = p - scale_friction if p > 0 else p
        risk_amt = min(c, cap_max) * risk_pct / 100
        c += pnl * risk_amt
        if c < 1:
            c = 1
        eq_pct.append((c - cap_init) / cap_init * 100)
        if c > peak:
            peak = c
        dd = (peak - c) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return eq_pct, c, max_dd


def tab_capital(results, signals):
    triggered = [r for r in results if r.triggered]
    triggered_r = [r.pnl_r for r in triggered]
    n_trades = len(triggered_r)

    if not triggered_r:
        st.info("無觸發交易")
        return

    m = compute_metrics(results)

    # ══════════════════════════════════════════════
    # 1. 頂部控制列
    # ══════════════════════════════════════════════
    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([1.2, 1.2, 1.2, 2])
    cap_init = ctrl1.number_input("初始資金 $", value=1000, step=100)
    pessimistic = ctrl2.toggle("悲觀模式", value=False, help="勝率 -10%，RR ×0.8")

    if pessimistic:
        ctrl2.caption("WR -10% | RR ×0.8")

    ctrl4.markdown(
        f'<div style="background:#161b22;border:1px solid #21262d;border-radius:4px;padding:8px 14px;'
        f'font-family:JetBrains Mono,monospace;font-size:0.7rem;display:flex;gap:20px;align-items:center">'
        f'<span style="color:#888">交易 <span style="color:#e6e6e6;font-weight:700">{n_trades:,}</span> 筆</span>'
        f'<span style="color:#888">勝率 <span style="color:#e6e6e6;font-weight:700">{m.win_rate:.1%}</span></span>'
        f'<span style="color:#888">RR <span style="color:#e6e6e6;font-weight:700">{m.avg_rr:.2f}</span></span>'
        f'<span style="color:#888">EV <span style="color:{GREEN if m.expectancy > 0 else RED};font-weight:700">'
        f'{m.expectancy:+.4f}R</span></span>'
        f'</div>', unsafe_allow_html=True)

    # 悲觀修正
    if pessimistic:
        rng = np.random.default_rng(123)
        adj_r = [p * 0.8 if p > 0 else p for p in triggered_r]
        n_extra = int(len(adj_r) * 0.1)
        adj_r.extend([-1.0] * n_extra)
        adj_r_arr = np.array(adj_r)
        rng.shuffle(adj_r_arr)
        sim_r = adj_r_arr.tolist()
    else:
        sim_r = triggered_r

    # ══════════════════════════════════════════════
    # 2. 核心區：曲線 + 數據矩陣（7:3）
    # ══════════════════════════════════════════════
    col_chart, col_matrix = st.columns([7, 3])

    risk_levels = [0.5, 1.0, 2.0, 3.0, 5.0]
    risk_colors = ["#555", BLUE, GREEN, YELLOW, RED]
    summary_rows = []
    dd_for_chart = None

    with col_chart:
        fig = make_subplots(rows=2, cols=1, row_heights=[0.75, 0.25],
                            shared_xaxes=True, vertical_spacing=0.03)

        for risk_pct, color in zip(risk_levels, risk_colors):
            eq_pct, final_c, max_dd = _sim_equity(sim_r, cap_init, risk_pct)
            ret_pct = (final_c - cap_init) / cap_init * 100
            width = 2.5 if risk_pct == 2.0 else 1

            fig.add_trace(go.Scatter(
                x=list(range(len(eq_pct))), y=eq_pct, mode="lines",
                name=f"{risk_pct}%", line=dict(color=color, width=width),
                hovertemplate=f"{risk_pct}% | " + "%{y:+,.1f}%<extra></extra>",
            ), row=1, col=1)

            if risk_pct == 2.0:
                eq_abs = [cap_init * (1 + p / 100) for p in eq_pct]
                eq_arr = np.array(eq_abs)
                pk = np.maximum.accumulate(eq_arr)
                dd_for_chart = np.where(pk > 0, (eq_arr - pk) / pk * 100, 0)

            status = "RUIN" if max_dd > 0.5 else "OK"
            summary_rows.append({
                "risk": risk_pct, "final": final_c, "ret": ret_pct,
                "dd": max_dd, "status": status, "color": color,
            })

        # 回撤子圖
        if dd_for_chart is not None:
            fig.add_trace(go.Scatter(
                x=list(range(len(dd_for_chart))), y=dd_for_chart.tolist(), mode="lines",
                line=dict(color=RED, width=1), fill="tozeroy",
                fillcolor="rgba(255,75,75,0.12)", showlegend=False,
                hovertemplate="DD: %{y:.1f}%<extra></extra>",
            ), row=2, col=1)
            fig.add_hline(y=-50, line_dash="dash", line_color=RED, opacity=0.6, row=2, col=1,
                          annotation_text="RUIN", annotation_position="bottom right",
                          annotation_font_color=RED, annotation_font_size=9)

        fig.add_hline(y=0, line_dash="dot", line_color="#333", opacity=0.3, row=1, col=1)
        fig.update_layout(
            template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
            margin=dict(l=45, r=10, t=10, b=30), height=380,
            font=dict(color=GRAY, size=10, family="JetBrains Mono, monospace"),
            legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1, font=dict(size=9)),
            hovermode="x unified",
            hoverlabel=dict(bgcolor="#0e1117", bordercolor="#333", font_size=10,
                            font_family="JetBrains Mono, monospace", font_color="#e6e6e6"),
        )
        fig.update_yaxes(title_text="收益 %", gridcolor="rgba(255,255,255,0.04)", griddash="dot", row=1, col=1)
        fig.update_yaxes(title_text="DD%", gridcolor="rgba(255,255,255,0.04)", griddash="dot", row=2, col=1)
        fig.update_xaxes(showgrid=False, row=1, col=1)
        fig.update_xaxes(title_text=f"交易筆數（{len(sim_r):,}）", showgrid=False, row=2, col=1)
        st.plotly_chart(fig, use_container_width=True)

    with col_matrix:
        # HTML 數據矩陣
        rows_html = ""
        for row in summary_rows:
            status_style = (
                f'background:rgba(255,75,75,0.2);color:{RED};font-weight:800'
                if row["status"] == "RUIN"
                else f'color:{GREEN}'
            )
            dd_color = RED if row["dd"] > 0.3 else (YELLOW if row["dd"] > 0.15 else "#888")
            ret_color = GREEN if row["ret"] > 0 else RED
            rows_html += (
                f'<tr style="border-bottom:1px solid #1a1f2b">'
                f'<td style="padding:6px 8px"><span style="color:{row["color"]};font-weight:700">{row["risk"]}%</span></td>'
                f'<td style="padding:6px 8px;text-align:right">{_fmt_money(row["final"])}</td>'
                f'<td style="padding:6px 8px;text-align:right;color:{ret_color}">{row["ret"]:+,.0f}%</td>'
                f'<td style="padding:6px 8px;text-align:right;color:{dd_color}">{row["dd"]:.0%}</td>'
                f'<td style="padding:6px 8px;text-align:center;{status_style}">{row["status"]}</td>'
                f'</tr>'
            )
        st.markdown(
            f'<table style="width:100%;border-collapse:collapse;font-family:JetBrains Mono,monospace;font-size:0.7rem;margin-top:4px">'
            f'<thead><tr style="border-bottom:1px solid #333;color:#555;font-size:0.55rem;text-transform:uppercase;letter-spacing:0.5px">'
            f'<th style="padding:5px 8px;text-align:left">風險</th>'
            f'<th style="padding:5px 8px;text-align:right">最終</th>'
            f'<th style="padding:5px 8px;text-align:right">報酬</th>'
            f'<th style="padding:5px 8px;text-align:right">DD</th>'
            f'<th style="padding:5px 8px;text-align:center">狀態</th>'
            f'</tr></thead><tbody>{rows_html}</tbody></table>',
            unsafe_allow_html=True,
        )

        # Kelly 摘要
        if m.avg_rr > 0 and m.win_rate > 0:
            kelly = m.win_rate - (1 - m.win_rate) / m.avg_rr
            kelly_pct = max(0, kelly * 100)
            ev_c = GREEN if m.expectancy > 0 else RED
            st.markdown(
                f'<div style="background:#161b22;border:1px solid #21262d;border-radius:4px;padding:10px 12px;'
                f'margin-top:12px;font-family:JetBrains Mono,monospace;font-size:0.65rem;color:#888">'
                f'<div style="margin-bottom:4px">Kelly <span style="color:white;font-weight:700">{kelly_pct:.1f}%</span>'
                f' → Half <span style="color:{GREEN};font-weight:700">{kelly_pct/2:.1f}%</span></div>'
                f'<div>EV <span style="color:{ev_c};font-weight:700">{m.expectancy:+.4f}R</span>'
                f' <span style="color:#444">| 含 0.1% 摩擦</span></div>'
                f'</div>', unsafe_allow_html=True)

    # ══════════════════════════════════════════════
    # 3. 底部診斷區：三欄並排
    # ══════════════════════════════════════════════
    st.divider()

    col_surv, col_sens, col_heat = st.columns(3)

    # ── 連虧生存率 ──
    with col_surv:
        streaks = [5, 10, 15, 20, 25]
        survival = [100 * ((1 - 2.0 / 100) ** s) for s in streaks]

        fig_s = go.Figure()
        colors_s = [GREEN if v > 80 else (YELLOW if v > 50 else RED) for v in survival]
        fig_s.add_trace(go.Bar(
            x=[f"{s}" for s in streaks], y=survival,
            marker_color=colors_s, text=[f"{v:.0f}%" for v in survival],
            textposition="outside", textfont=dict(size=9),
        ))
        fig_s.add_hline(y=50, line_dash="dash", line_color=RED, opacity=0.5)
        fig_s.update_layout(**playout("連虧生存率 (2%)", 220), showlegend=False)
        fig_s.update_yaxes(title_text="%", range=[0, 105])
        fig_s.update_xaxes(title_text="連虧次數")
        st.plotly_chart(fig_s, use_container_width=True)

    # ── 勝率衰減敏感度 ──
    with col_sens:
        wr_offsets = [-0.10, -0.05, -0.03, 0, +0.03, +0.05]
        exp_values = []
        for offset in wr_offsets:
            wr = max(0, min(1, m.win_rate + offset))
            exp_values.append(wr * m.avg_win_r + (1 - wr) * m.avg_loss_r)

        colors_d = [GREEN if v > 0 else RED for v in exp_values]
        labels = [f"{o:+.0%}" if o != 0 else "NOW" for o in wr_offsets]

        fig_d = go.Figure()
        fig_d.add_trace(go.Bar(
            x=labels, y=exp_values, marker_color=colors_d,
            text=[f"{v:+.3f}" for v in exp_values],
            textposition="outside", textfont=dict(size=9),
        ))
        fig_d.add_hline(y=0, line_color="#555", line_width=1)
        fig_d.update_layout(**playout("勝率衰減 → EV", 220), showlegend=False)
        fig_d.update_yaxes(title_text="EV (R)")
        fig_d.update_xaxes(title_text="勝率偏移")
        st.plotly_chart(fig_d, use_container_width=True)

    # ── 參數穩定性熱力圖 ──
    with col_heat:
        wr_range = np.arange(0.10, 0.50, 0.02)
        rr_range = np.arange(0.5, 5.5, 0.25)
        hmap = np.array([[wr * rr - (1 - wr) for wr in wr_range] for rr in rr_range])

        fig_h = go.Figure(go.Heatmap(
            z=hmap, x=[f"{w:.0%}" for w in wr_range], y=[f"{r:.1f}" for r in rr_range],
            colorscale=[[0, RED], [0.4, "#1a1a2e"], [0.5, "#333"], [0.6, "#1a2e1a"], [1, GREEN]],
            zmin=-1, zmax=2, zmid=0, colorbar=dict(title="EV", len=0.8, thickness=10),
            hovertemplate="WR:%{x} RR:%{y}<br>EV:%{z:.3f}R<extra></extra>",
        ))
        fig_h.add_trace(go.Scatter(
            x=[f"{m.win_rate:.0%}"], y=[f"{m.avg_rr:.1f}"],
            mode="markers+text", text=["YOU"],
            textposition="top center", textfont=dict(color="white", size=10, family="JetBrains Mono"),
            marker=dict(size=12, color="white", symbol="circle-open", line=dict(width=2)),
            showlegend=False,
        ))
        fig_h.update_layout(**playout("EV 地圖", 220), showlegend=False)
        fig_h.update_xaxes(title_text="勝率", tickangle=-45, dtick=4)
        fig_h.update_yaxes(title_text="RR")
        st.plotly_chart(fig_h, use_container_width=True)


def tab_verdict(results, signals):
    m = compute_metrics(results)
    v = evaluate_edge(m)

    if v.has_edge and v.confidence >= 0.7:
        st.markdown(f'<div class="verdict-card v-pass">PASS<br><span style="font-size:0.9rem">Confidence {v.confidence:.0%}</span></div>', unsafe_allow_html=True)
    elif not v.has_edge:
        st.markdown(f'<div class="verdict-card v-fail">FAIL<br><span style="font-size:0.9rem">Confidence {v.confidence:.0%}</span></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="verdict-card v-observe">OBSERVE<br><span style="font-size:0.9rem">Confidence {v.confidence:.0%}</span></div>', unsafe_allow_html=True)

    cr, cd = st.columns([1, 1])

    with cr:
        stab = min(1, max(0, 1 - m.max_drawdown_r / max(abs(m.total_r), 1)))
        prof = min(1, max(0, (m.expectancy + 0.5) / 1.0))
        lat = 0.8 if m.expectancy > 0.15 else (0.5 if m.expectancy > 0.05 else 0.2)
        freq = min(1, m.triggered_count / 200)
        risk_c = min(1, max(0, 1 - m.tp1_hit_then_sl_rate))

        cats = ["一致性", "獲利能力", "延遲容忍", "信號頻率", "風控"]
        vals = [stab * 100, prof * 100, lat * 100, freq * 100, risk_c * 100]

        fig_r = go.Figure()
        fig_r.add_trace(go.Scatterpolar(
            r=vals + [vals[0]], theta=cats + [cats[0]], fill="toself",
            fillcolor="rgba(41,98,255,0.15)", line=dict(color=BLUE, width=2),
        ))
        fig_r.update_layout(
            polar=dict(
                bgcolor=BG,
                radialaxis=dict(range=[0, 100], gridcolor=BORDER, tickfont=dict(size=8)),
                angularaxis=dict(gridcolor=BORDER),
            ),
            template="plotly_dark", paper_bgcolor=BG, height=300,
            margin=dict(l=50, r=50, t=20, b=20),
            font=dict(color=GRAY, size=10), showlegend=False,
        )
        st.plotly_chart(fig_r, use_container_width=True)

    with cd:
        st.markdown("**優勢**")
        if m.expectancy > 0:
            st.markdown(f"[+] 正期望值 ({m.expectancy:+.4f}R)")
        if m.avg_rr > 1.5:
            st.markdown(f"[+] 良好 RR ({m.avg_rr:.2f})")
        if m.tp1_hit_then_sl_rate < 0.2:
            st.markdown(f"[+] TP1後SL低 ({m.tp1_hit_then_sl_rate:.0%})")
        if m.max_consecutive_losses < 8:
            st.markdown(f"[+] 連敗可控 ({m.max_consecutive_losses})")
        if m.triggered_count > 50:
            st.markdown(f"[+] 足夠樣本 ({m.triggered_count})")

        st.markdown("")
        st.markdown("**缺點**")
        if m.expectancy <= 0:
            st.markdown(f"[-] 負期望值 ({m.expectancy:+.4f}R)")
        if m.avg_rr < 1:
            st.markdown(f"[-] RR 太低 ({m.avg_rr:.2f})")
        if m.tp1_hit_then_sl_rate > 0.3:
            st.markdown(f"[-] TP1後SL高 ({m.tp1_hit_then_sl_rate:.0%})")
        if m.max_consecutive_losses >= 8:
            st.markdown(f"[-] 長連敗 ({m.max_consecutive_losses})")
        if m.max_drawdown_r > 20:
            st.markdown(f"[-] 大回撤 ({m.max_drawdown_r:.1f}R)")
        if m.triggered_count < 30:
            st.markdown(f"[-] 樣本不足 ({m.triggered_count})")

        for r in v.reasons:
            st.caption(f">> {r}")
        for w in v.warnings:
            st.caption(f"!! {w}")


def tab_research(results, signals):
    """回測分析相關功能"""
    sub1, sub2, sub3, sub4 = st.tabs(["回測", "Edge 分析", "資本模擬", "裁決"])
    with sub1:
        tab_backtest(results, signals)
    with sub2:
        tab_edge(results, signals)
    with sub3:
        tab_capital(results, signals)
    with sub4:
        tab_verdict(results, signals)

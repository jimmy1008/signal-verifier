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


def tab_capital(results, signals):
    risk_d = st.session_state.get("risk", 1.0)
    c1, c2, c3, c4 = st.columns(4)
    cap_init = c1.number_input("初始資金 $", value=10000, step=1000)
    risk_v = c2.number_input("風險 %", value=risk_d, step=0.1)
    cost = c3.number_input("成本 (R)", value=0.10, step=0.01)
    mc_n = c4.number_input("模擬次數", value=100, step=50, min_value=10)

    triggered_r = [r.pnl_r - cost for r in results if r.triggered]
    if not triggered_r:
        st.info("無觸發交易")
        return

    adj = [
        TradeResult(
            signal_id=r.signal_id, triggered=r.triggered, entry_time=r.entry_time,
            exit_time=r.exit_time, exit_reason=r.exit_reason, max_tp_hit=r.max_tp_hit,
            pnl_r=(r.pnl_r - cost) if r.triggered else 0,
        )
        for r in results
    ]
    cap = run_simulation(adj, float(cap_init), risk_v / 100)

    vmap = {
        "viable": (GREEN, "可交易"),
        "untradeable": (RED, "不可交易"),
        "psychologically_untradeable": (YELLOW, "心理不可承受"),
        "no_edge": (RED, "無優勢"),
    }
    vc, vt = vmap.get(cap.verdict, (GRAY, "?"))

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("最終資金", f"${cap.final_capital:,.0f}")
    mc2.metric("報酬", f"{cap.total_return_pct:+.1%}")
    mc3.metric("最大回撤", f"{cap.max_drawdown_pct:.1%}")
    mc4.metric("連敗", cap.max_losing_streak)
    mc5.metric("谷底", f"{cap.min_capital_ratio:.0%}")

    # Monte Carlo
    rng = np.random.default_rng(42)
    r_arr = np.array(triggered_r)
    n_t = len(r_arr)

    fig = go.Figure()
    finals = []
    bust = 0

    for i in range(int(mc_n)):
        shuf = rng.choice(r_arr, n_t, replace=True)
        eq = [float(cap_init)]
        c = float(cap_init)
        for p in shuf:
            c *= (1 + p * risk_v / 100)
            eq.append(c)
        finals.append(c)
        if c < cap_init * 0.5:
            bust += 1
        color = GREEN if c >= cap_init else RED
        fig.add_trace(go.Scatter(
            x=list(range(len(eq))), y=eq, mode="lines",
            line=dict(width=0.5, color=color), opacity=0.08, showlegend=False,
        ))

    fig.add_hline(y=float(np.median(finals)), line_dash="dash", line_color=YELLOW,
                  annotation_text=f"中位數 ${np.median(finals):,.0f}")
    fig.add_hline(y=cap_init, line_dash="dot", line_color=GRAY, annotation_text="初始")
    fig.update_layout(**playout(f"蒙地卡羅 {int(mc_n)} 次模擬", 320))
    st.plotly_chart(fig, use_container_width=True)

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("破產機率", f"{bust / mc_n * 100:.1f}%")
    sc2.metric("中位數", f"${np.median(finals):,.0f}")
    sc3.metric("最佳", f"${np.max(finals):,.0f}")
    sc4.metric("最差", f"${np.min(finals):,.0f}")

    m = compute_metrics(results)
    if m.avg_rr > 0 and m.win_rate > 0:
        kelly = m.win_rate - (1 - m.win_rate) / m.avg_rr
        kelly_pct = max(0, kelly * 100)
        st.markdown(f"""
        <div class="kelly-box">
            <b>Kelly 準則</b><br>
            最佳比例: <b>{kelly_pct:.1f}%</b> | 保守建議 (Half Kelly): <b>{kelly_pct / 2:.1f}%</b>
        </div>
        """, unsafe_allow_html=True)


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

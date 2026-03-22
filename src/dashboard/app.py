"""Signal Verifier Dashboard v5"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from src.database import get_session
from src.models import BacktestRunORM
from src.stats.metrics import compute_metrics

from src.dashboard.helpers import (
    BG, CARD, BORDER, GREEN, RED, YELLOW, BLUE, GRAY, TEXT,
    setup_db, clean_results, load_results, load_signals,
    start_bot, stop_bot, bot_status,
)
from src.dashboard.tab_operations import tab_exec, _show_profit_card
from src.dashboard.tab_signals import tab_signals
from src.dashboard.tab_closed import tab_closed
from src.dashboard.tab_research import tab_research

st.set_page_config(page_title="Signal Verifier", page_icon="SV", layout="wide")


def _inject_css():
    """注入全域 CSS 樣式"""
    st.markdown(f"""
<style>
    /* === 背景漸變 === */
    .stApp {{
        background: linear-gradient(135deg, #0f1117 0%, #050508 100%);
    }}

    .block-container {{ padding: 0.2rem 0.8rem 0.5rem 0.8rem; }}
    footer {{ visibility: hidden !important; }}
    #MainMenu {{ display: none !important; }}
    .stDeployButton {{ display: none !important; }}
    [data-testid="collapsedControl"] {{ display: none !important; }}
    [data-testid="stSidebar"] {{ transform: none !important; }}
    header[data-testid="stHeader"] {{ background: transparent !important; }}


    /* === Metric 卡片 === */
    [data-testid="stMetric"] {{
        background: #1e222d;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 12px 16px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.6);
        transition: border-color 0.3s ease;
    }}
    [data-testid="stMetricLabel"] {{
        font-size: 0.55rem !important;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 1px;
        font-weight: 600;
    }}
    [data-testid="stMetricValue"] {{
        font-size: 1.1rem !important;
        font-family: 'JetBrains Mono', 'Roboto Mono', 'Consolas', monospace !important;
        color: #e6e6e6 !important;
    }}

    /* === 帳戶區塊色帶 === */
    .acct-h1 {{
        border-left: 4px solid {BLUE};
        padding-left: 12px;
        margin-bottom: 8px;
    }}
    .acct-h4 {{
        border-left: 4px solid {YELLOW};
        padding-left: 12px;
        margin-bottom: 8px;
    }}

    /* === 方向標籤 === */
    .tag-long {{
        background: rgba(0,200,5,0.15);
        color: {GREEN};
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.7rem;
    }}
    .tag-short {{
        background: rgba(255,75,75,0.15);
        color: {RED};
        padding: 2px 8px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.7rem;
    }}

    /* === 保證金進度條 === */
    .margin-bar {{
        background: #1a1f2b;
        border-radius: 4px;
        height: 6px;
        margin-top: 4px;
        overflow: hidden;
    }}
    .margin-fill {{
        height: 100%;
        border-radius: 4px;
        transition: width 0.5s ease;
    }}
    .margin-fill-safe {{ background: {BLUE}; }}
    .margin-fill-warn {{ background: {YELLOW}; }}
    .margin-fill-danger {{ background: {RED}; }}

    /* === 浮盈卡動態頂邊 === */
    .pnl-card-pos {{
        border-top: 2px solid {GREEN};
        background: rgba(0,200,5,0.05);
    }}
    .pnl-card-neg {{
        border-top: 2px solid {RED};
        background: rgba(255,75,75,0.05);
    }}

    /* === H1/H4 分隔線 === */
    .acct-separator {{
        border-right: 1px solid #30363d;
        padding-right: 16px;
    }}

    /* === Tab 下劃線模式 === */
    .stTabs [data-baseweb="tab-list"] {{
        gap: 2px;
        background: transparent;
        border-bottom: 1px solid {BORDER};
        padding: 0;
        border-radius: 0;
    }}
    .stTabs [data-baseweb="tab"] {{
        padding: 10px 18px;
        font-size: 0.8rem;
        border-radius: 0;
        font-weight: 500;
        border-bottom: 2px solid transparent;
    }}
    .stTabs [aria-selected="true"] {{
        background: transparent !important;
        border-bottom: 2px solid {BLUE} !important;
        box-shadow: 0 2px 8px rgba(41,98,255,0.2);
        color: white !important;
    }}

    /* === Sidebar === */
    [data-testid="stSidebar"] {{
        min-width: 250px;
        max-width: 270px;
        background: linear-gradient(180deg, #0d1117 0%, #0a0d12 100%);
        border-right: 1px solid #1a1f2b;
    }}
    [data-testid="stSidebar"] > div:first-child {{ padding-top: 0.3rem !important; }}
    section[data-testid="stSidebar"] > div {{ padding-top: 0.5rem; }}
    [data-testid="stSidebar"] [data-testid="stMetric"] {{
        padding: 4px 8px;
        background: #151a24;
        border: 1px solid #1e2530;
    }}
    [data-testid="stSidebar"] [data-testid="stMetricValue"] {{ font-size: 0.8rem !important; }}
    [data-testid="stSidebar"] h5 {{
        letter-spacing: 1.5px;
        font-size: 0.7rem !important;
        color: #888;
    }}
    [data-testid="stSidebar"] hr {{
        border-color: #1a1f2b;
        margin: 10px 0;
    }}

    /* === LED 呼吸燈 === */
    .led {{
        display: inline-block;
        width: 8px;
        height: 8px;
        border-radius: 50%;
        margin-right: 6px;
    }}
    .led-on {{
        background: {GREEN};
        box-shadow: 0 0 4px {GREEN}, 0 0 10px rgba(0,200,5,0.4);
        animation: pulse-green 2s ease-in-out infinite;
    }}
    .led-off {{
        background: #555;
        box-shadow: 0 0 2px #555;
    }}
    @keyframes pulse-green {{
        0%, 100% {{ box-shadow: 0 0 4px {GREEN}; }}
        50% {{ box-shadow: 0 0 12px {GREEN}, 0 0 24px rgba(0,200,5,0.3); }}
    }}

    /* === 判決卡 === */
    .verdict-card {{
        text-align: center;
        padding: 24px;
        border-radius: 12px;
        font-size: 1.8rem;
        font-weight: 800;
        margin-bottom: 12px;
    }}
    .v-pass {{ background: #0a2e1a; color: {GREEN}; border: 2px solid {GREEN}; box-shadow: 0 0 20px rgba(0,200,5,0.15); }}
    .v-fail {{ background: #2e0a0a; color: {RED}; border: 2px solid {RED}; box-shadow: 0 0 20px rgba(255,75,75,0.15); }}
    .v-observe {{ background: #2e1f0a; color: {YELLOW}; border: 2px solid {YELLOW}; }}

    /* === Kelly === */
    .kelly-box {{
        background: #1e222d;
        border: 1px solid {BLUE};
        border-radius: 10px;
        padding: 14px 18px;
        margin-top: 8px;
    }}

    /* === 控制按鈕 === */
    .ctrl-bar {{
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 8px;
    }}
    .btn-pill {{
        display: inline-block;
        padding: 6px 20px;
        border-radius: 20px;
        font-weight: 700;
        font-size: 0.78rem;
        cursor: pointer;
        text-align: center;
        letter-spacing: 0.5px;
    }}
    .btn-go {{
        background: #0d3320;
        color: {GREEN};
        border: 1px solid {GREEN};
        box-shadow: 0 0 8px rgba(0,200,5,0.15);
    }}
    .btn-stop {{
        background: #3d1014;
        color: {RED};
        border: 1px solid {RED};
        box-shadow: 0 0 8px rgba(255,75,75,0.15);
    }}

    /* === Log 區塊 === */
    .log-box {{
        background: #050505;
        border-left: 3px solid {BLUE};
        border-radius: 6px;
        padding: 10px 14px;
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        font-size: 0.68rem;
        line-height: 1.6;
        color: #8b949e;
        height: calc(100vh - 220px);
        overflow-y: auto;
    }}
    .log-box .log-time {{ color: #555; }}
    .log-box .log-info {{ color: #58a6ff; }}
    .log-box .log-msg {{ color: #aab; }}
    .log-box .log-tp {{ color: {GREEN}; font-weight: 700; }}
    .log-box .log-sl {{ color: {RED}; font-weight: 700; }}
    .log-box .log-warn {{ color: {YELLOW}; font-size: 0.6rem; opacity: 0.6; }}

    /* === 表格 === */
    .stDataFrame {{
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        font-size: 0.78rem;
    }}
    .stDataFrame td {{
        padding: 8px 12px !important;
    }}
    .stDataFrame th {{
        font-weight: 700 !important;
        text-transform: uppercase;
        font-size: 0.6rem !important;
        letter-spacing: 0.8px;
        color: #666 !important;
        border-bottom: 1px solid #333 !important;
    }}

    /* === 浮盈高亮 === */
    .pnl-pos {{ color: {GREEN}; font-weight: 700; font-family: 'JetBrains Mono', monospace; }}
    .pnl-neg {{ color: {RED}; font-weight: 700; font-family: 'JetBrains Mono', monospace; }}
    .pnl-zero {{ color: #555; font-family: 'JetBrains Mono', monospace; }}

    /* === 分享按鈕 === */
    .share-btn {{
        display: flex;
        align-items: flex-end;
        height: 100%;
    }}
    .share-btn button {{
        border: 1px solid #444 !important;
        border-radius: 50% !important;
        background: #1e222d !important;
        width: 36px !important;
        height: 36px !important;
        min-height: 36px !important;
        padding: 0 !important;
        font-size: 1.1rem !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        cursor: pointer !important;
        transition: all 0.2s !important;
    }}
    .share-btn button:hover {{
        border-color: {BLUE} !important;
        background: #252b38 !important;
    }}
</style>
""", unsafe_allow_html=True)


_inject_css()


# ── Sidebar ──────────────────────────────────────────

def sidebar():
    setup_db()
    st.sidebar.markdown("### Signal Verifier")
    s = get_session()
    runs = s.query(BacktestRunORM).order_by(BacktestRunORM.created_at.desc()).all()
    s.close()

    if not runs:
        st.sidebar.warning("尚無回測資料")
        return None, None

    nm = {
        "cfd_best": "外匯 / CFD",
        "crypto_best": "加密貨幣",
    }
    opts = {nm.get(r.config_name, r.config_name): r.id for r in runs}
    sel = st.sidebar.selectbox("回測組", list(opts.keys()), label_visibility="collapsed")
    results = load_results(opts[sel])
    signals = load_signals()

    if results:
        m = compute_metrics(clean_results(results))
        a, b = st.sidebar.columns(2)
        a.metric("期望值", f"{m.expectancy:+.4f}R")
        b.metric("勝率", f"{m.win_rate:.1%}")
        a.metric("累積 R", f"{m.total_r:+.1f}")
        b.metric("信號數", m.total_signals)

    st.sidebar.divider()
    with st.sidebar.expander("策略規則"):
        st.caption("風險: 2% / 筆")
        st.caption("TP1: 不出場")
        st.caption("TP2: SL 移到 Entry（保本含手續費）")
        st.caption("TP3: 出 50%")
        st.caption("TP4: 出 50%（全平）")
        st.caption("SL 距離 < 0.1%: 不下單")
        st.caption("最大持倉: 無上限")

    st.sidebar.divider()
    st.sidebar.markdown("##### Broker")
    from src.config import load_config as _lc_sidebar
    _sb_cfg = _lc_sidebar()
    _sb_bingx = _sb_cfg.get("bingx", {})
    _sb_oanda = _sb_cfg.get("oanda", {})
    _bingx_ok = _sb_bingx.get("api_key") and _sb_bingx["api_key"] != "YOUR_BINGX_API_KEY"
    _oanda_ok = _sb_oanda.get("api_token") and _sb_oanda["api_token"] != "YOUR_OANDA_API_TOKEN"
    _has_sub = bool(_sb_bingx.get("sub_api_key"))

    if _bingx_ok:
        st.sidebar.markdown(
            f'<span class="led led-on"></span> BingX H1（主帳戶）',
            unsafe_allow_html=True)
        if _has_sub:
            st.sidebar.markdown(
                f'<span class="led led-on"></span> BingX H4（子帳戶）',
                unsafe_allow_html=True)
    else:
        st.sidebar.markdown(
            f'<span class="led led-off"></span> BingX 未連線',
            unsafe_allow_html=True)

    if _oanda_ok:
        st.sidebar.markdown(
            f'<span class="led led-on"></span> OANDA（外匯）',
            unsafe_allow_html=True)
    else:
        st.sidebar.markdown(
            f'<span class="led led-off"></span> <span style="color:#555">OANDA 未設定</span>',
            unsafe_allow_html=True)

    st.sidebar.divider()
    st.sidebar.markdown("##### Bot 控制")
    _status = bot_status()
    if _status == "running":
        st.sidebar.markdown('<span class="led led-on"></span> **運行中**', unsafe_allow_html=True)
    else:
        st.sidebar.markdown('<span class="led led-off"></span> 已停止', unsafe_allow_html=True)

    _sb1, _sb2 = st.sidebar.columns(2)
    with _sb1:
        if _status != "running":
            if st.button("START", key="sb_start", use_container_width=True):
                start_bot()
                import time; time.sleep(2); st.rerun()
        else:
            st.button("START", disabled=True, key="sb_start_d", use_container_width=True)
    with _sb2:
        if _status == "running":
            if st.button("STOP", key="sb_stop", use_container_width=True):
                stop_bot()
                import time; time.sleep(1); st.rerun()
        else:
            st.button("STOP", disabled=True, key="sb_stop_d", use_container_width=True)

    st.sidebar.divider()
    sb_c1, sb_c2 = st.sidebar.columns(2)
    with sb_c1:
        if st.button("↗ 收益概覽", key="sb_share", use_container_width=True):
            _show_profit_card()
    with sb_c2:
        if st.button("↻ 刷新", key="sb_refresh", use_container_width=True):
            st.rerun()

    return results, signals


# ── Main ─────────────────────────────────────────────

def main():
    try:
        r, s = sidebar()
    except Exception as e:
        st.error(f"Sidebar 錯誤: {e}")
        import traceback
        st.code(traceback.format_exc())
        r, s = None, None

    if not r:
        st.warning("請先執行回測（或 sidebar 載入失敗）")

    # tabs 永遠渲染（不管 sidebar 是否成功）
    t1, t2, t3, t4 = st.tabs([
        "執行總覽", "信號動態", "已平倉", "研究室",
    ])
    with t1:
        tab_exec(r, s)
    with t2:
        if r:
            tab_signals(r, s)
        else:
            st.info("等待回測資料...")
    with t3:
        tab_closed(r, s)
    with t4:
        if r:
            tab_research(r, s)
        else:
            st.info("等待回測資料...")


if __name__ == "__main__":
    main()

"""Dashboard 共用常數、API 快取、工具函式"""

from __future__ import annotations

import time as _time
from pathlib import Path

import numpy as np
import streamlit as st

from src.database import init_db, get_session
from src.models import BacktestRunORM, TradeResultORM, SignalORM
from src.stats.metrics import TradeResult, ExitReason

# ── 常數 ──────────────────────────────────────────────

TRADE_START_TS = 1774027800000  # 2026-03-21 01:30 UTC+8 (ms)

BG = "#0e1117"
CARD = "#161b22"
BORDER = "#21262d"
GREEN = "#00C805"
RED = "#FF4B4B"
YELLOW = "#FF9000"
BLUE = "#2962ff"
GRAY = "#8b949e"
TEXT = "#c9d1d9"


# ── Plotly 佈局 ──────────────────────────────────────

def playout(title="", h=320):
    return dict(
        template="plotly_dark", paper_bgcolor=BG, plot_bgcolor=BG,
        title=dict(text=title, font=dict(size=13, color=TEXT)),
        margin=dict(l=40, r=15, t=30, b=25), height=h,
        font=dict(color=GRAY, size=10),
        xaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER),
        yaxis=dict(gridcolor=BORDER, zerolinecolor=BORDER),
    )


# ── DB 工具 ──────────────────────────────────────────

@st.cache_resource
def setup_db():
    init_db()
    return True


def clean_results(results):
    return [r for r in results if not (r.triggered and r.exit_reason and r.exit_reason == ExitReason.EXPIRED)]


def load_results(rid):
    s = get_session()
    rows = s.query(TradeResultORM).filter_by(run_id=rid).order_by(TradeResultORM.id).all()
    out = [
        TradeResult(
            signal_id=r.signal_id, triggered=r.triggered, entry_time=r.entry_time,
            exit_time=r.exit_time,
            exit_reason=ExitReason(r.exit_reason.value) if r.exit_reason else None,
            exit_price=r.exit_price or 0, max_tp_hit=r.max_tp_hit or 0,
            pnl_r=r.pnl_r or 0, pnl_pct=r.pnl_pct or 0,
            drawdown_r=r.drawdown_r or 0, notes=r.notes or "",
        )
        for r in rows
    ]
    s.close()
    return out


def load_signals():
    s = get_session()
    sigs = s.query(SignalORM).all()
    out = {x.id: x for x in sigs}
    s.close()
    return out


# ── API 快取 ─────────────────────────────────────────

_api_cache: dict = {}
_CACHE_TTL_ACCOUNT = 30
_CACHE_TTL_TRADES = 120


@st.cache_resource
def _get_ccxt_instance(api_key: str, api_secret: str):
    """快取 ccxt 實例（不重複建立）"""
    import ccxt
    return ccxt.bingx({
        "apiKey": api_key, "secret": api_secret,
        "options": {"defaultType": "swap"},
    })


def _fetch_one_bingx(api_key, api_secret, cache_label="", fetch_trades=False):
    """從一個 BingX 帳戶取得帳戶 + 持倉 + 成交（帶快取）"""
    now = _time.time()
    cache_key = f"bingx_{cache_label}"
    cached = _api_cache.get(cache_key)

    if cached and (now - cached["ts"]) < _CACHE_TTL_ACCOUNT:
        if not fetch_trades:
            return cached["account"], cached["positions"], [], cached["ex"]
        trades_cached = _api_cache.get(f"{cache_key}_trades")
        if trades_cached and (now - trades_cached["ts"]) < _CACHE_TTL_TRADES:
            return cached["account"], cached["positions"], trades_cached["data"], cached["ex"]
        ex = cached["ex"]
        account = cached["account"]
        positions_raw = cached["positions_raw"]
        open_pos = cached["positions"]
    else:
        ex = _get_ccxt_instance(api_key, api_secret)

        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {})
        account = {
            "balance": float(usdt.get("total", 0)),
            "available": float(usdt.get("free", 0)),
            "used": float(usdt.get("used", 0)),
        }

        positions_raw = ex.fetch_positions()
        open_pos = []
        total_upnl = 0.0
        for pos in positions_raw:
            contracts = abs(float(pos.get("contracts", 0)))
            if contracts > 0:
                upnl = float(pos.get("unrealizedPnl", 0))
                total_upnl += upnl
                entry_p = float(pos.get("entryPrice", 0))
                mark_p = float(pos.get("markPrice", 0))
                liq_p = pos.get("liquidationPrice")
                leverage = pos.get("leverage", "?")

                pnl_pct = 0.0
                if entry_p > 0:
                    if pos.get("side") == "long":
                        pnl_pct = (mark_p - entry_p) / entry_p * 100
                    else:
                        pnl_pct = (entry_p - mark_p) / entry_p * 100

                liq_str = "-"
                if liq_p is not None:
                    try:
                        liq_str = f"{float(liq_p):.4f}"
                    except (ValueError, TypeError):
                        liq_str = "-"

                lev_num = float(leverage) if str(leverage).replace('.', '').isdigit() else 20
                notional = contracts * entry_p
                margin = notional / lev_num if lev_num > 0 else notional

                open_pos.append({
                    "商品": pos.get("symbol", ""),
                    "方向": "LONG" if pos.get("side") == "long" else "SHORT",
                    "保證金": f"${margin:.2f}",
                    "進場價": f"{entry_p:.4f}",
                    "標記價": f"{mark_p:.4f}",
                    "浮盈": f"${upnl:+.4f}",
                    "盈虧%": f"{pnl_pct:+.2f}%",
                    "槓桿": f"{leverage}x",
                    "強平價": liq_str,
                })

        account["unrealized_pnl"] = total_upnl
        account["equity"] = account["balance"] + total_upnl

        _api_cache[cache_key] = {
            "account": account, "positions": open_pos,
            "positions_raw": positions_raw, "ex": ex, "ts": now,
        }

    if not fetch_trades:
        return account, open_pos, [], ex

    trades_cache_key = f"{cache_key}_trades"
    trades_cached = _api_cache.get(trades_cache_key)
    if trades_cached and (now - trades_cached["ts"]) < _CACHE_TTL_TRADES:
        recent_trades = trades_cached["data"]
    else:
        recent_trades = []
        try:
            traded_symbols = set()
            for pos in positions_raw:
                sym = pos.get("symbol", "")
                if sym:
                    traded_symbols.add(sym)
            traded_symbols.update(["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"])
            for sym in list(traded_symbols)[:10]:
                try:
                    trades = ex.fetch_my_trades(sym, limit=10)
                    recent_trades.extend(trades)
                    _time.sleep(0.3)
                except Exception as _e:
                    _log.debug(f"靜默異常: {_e}")
                    pass
            recent_trades = [t for t in recent_trades if t.get("timestamp", 0) >= TRADE_START_TS]
            recent_trades.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            recent_trades = recent_trades[:30]
        except Exception as _e:
            _log.debug(f"靜默異常: {_e}")
            pass
        _api_cache[trades_cache_key] = {"data": recent_trades, "ts": now}

    return account, open_pos, recent_trades, ex


def get_bingx_data(fetch_trades=False):
    """從 BingX 取得雙帳戶資料"""
    try:
        from src.config import load_config
        config = load_config()
        bingx_cfg = config.get("bingx", {})

        if not bingx_cfg.get("api_key") or bingx_cfg["api_key"] == "YOUR_BINGX_API_KEY":
            return None, None, None, None, None, None, None

        h1_account, h1_positions, h1_trades, h1_ex = _fetch_one_bingx(
            bingx_cfg["api_key"], bingx_cfg["api_secret"],
            cache_label="h1", fetch_trades=fetch_trades)

        h4_account, h4_positions, h4_trades, h4_ex = None, None, None, None
        if bingx_cfg.get("sub_api_key"):
            _time.sleep(1)
            h4_account, h4_positions, h4_trades, h4_ex = _fetch_one_bingx(
                bingx_cfg["sub_api_key"], bingx_cfg["sub_api_secret"],
                cache_label="h4", fetch_trades=fetch_trades)

        return h1_account, h1_positions, h1_trades, h4_account, h4_positions, h4_trades, h1_ex

    except Exception as e:
        return {"error": str(e)}, None, None, None, None, None, None


# ── Bot 控制 ─────────────────────────────────────────

def get_project_root():
    return str(Path(__file__).resolve().parent.parent.parent)


def start_bot():
    import subprocess, os
    root = get_project_root()
    log_file = open(f"{root}/auto_trade.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    subprocess.Popen(
        ["python", "scripts/auto_trade.py"],
        cwd=root,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
    )


def stop_bot():
    import subprocess
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%' and commandline like '%auto_trade%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                subprocess.run(["taskkill", "/PID", line, "/F"], timeout=5)
                return True
    except Exception as _e:
        _log.debug(f"靜默異常: {_e}")
        pass
    return False


def bot_status():
    import subprocess
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%' and commandline like '%auto_trade%'",
             "get", "processid"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if line.strip().isdigit():
                return "running"
    except Exception as _e:
        _log.debug(f"靜默異常: {_e}")
        pass
    return "stopped"


# ── 已平倉重建 ──────────────────────────────────────

def build_closed_positions(trades):
    """從成交紀錄重建已平倉位，用 Decimal 精度計算盈虧"""
    from datetime import datetime as _dt
    from decimal import Decimal, ROUND_HALF_UP
    import json as _json
    import logging

    _log = logging.getLogger(__name__)

    be_marks = {}
    try:
        be_path = Path(__file__).resolve().parent.parent.parent / "db" / "be_marks.json"
        if be_path.exists():
            be_marks = _json.loads(be_path.read_text(encoding="utf-8"))
    except Exception as _e:
        _log.debug(f"靜默異常: {_e}")
        pass

    opens = {}  # key: (sym, posSide) → {notional: Decimal, fee: Decimal, price: Decimal}
    closed = []
    seen = set()

    D = Decimal  # 簡寫

    for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
        try:
            info = t.get("info", {})
            pos_side = info.get("positionSide", "")
            side = str(t.get("side", "")).lower()
            sym = str(t.get("symbol", "")).replace("/USDT:USDT", "").replace("-", "")
            price = D(str(t.get("price", 0) or 0))
            fee = abs(D(str(info.get("commission", 0) or 0)))
            ts = _dt.fromtimestamp(t.get("timestamp", 0) / 1000)
            notional = abs(D(str(info.get("amount", 0) or 0)))
            if notional == 0:
                notional = abs(D(str(t.get("cost", 0) or 0)))

            is_open = (pos_side == "LONG" and side == "buy") or (pos_side == "SHORT" and side == "sell")
            is_close = (pos_side == "LONG" and side == "sell") or (pos_side == "SHORT" and side == "buy")
            k = (sym, pos_side)

            if is_open:
                if k not in opens:
                    opens[k] = {"notional": notional, "fee": fee, "price": price}
                else:
                    old = opens[k]
                    total_n = old["notional"] + notional
                    old["price"] = (old["notional"] * old["price"] + notional * price) / total_n if total_n > 0 else price
                    old["notional"] = total_n
                    old["fee"] += fee
            elif is_close:
                oid = info.get("orderId", "")
                if oid in seen:
                    continue
                seen.add(oid)

                entry_info = opens.get(k)
                avg_entry = entry_info["price"] if entry_info and entry_info["notional"] > 0 else price
                entry_fee = entry_info["fee"] if entry_info else D("0")

                equiv_qty = notional / price if price > 0 else D("0")
                if pos_side == "LONG":
                    raw_pnl = (price - avg_entry) * equiv_qty
                else:
                    raw_pnl = (avg_entry - price) * equiv_qty

                net_pnl = raw_pnl - fee - entry_fee

                order_type = str(info.get("type", "")).upper()
                be_key = f"{sym}USDT.P|{pos_side.lower()}"
                is_be_marked = be_marks.get(be_key, False)

                if "TAKE_PROFIT" in order_type:
                    exit_type = "TP"
                elif "STOP" in order_type:
                    exit_type = "BE" if is_be_marked else "SL"
                else:
                    if is_be_marked and abs(raw_pnl) < D("0.5"):
                        exit_type = "BE"
                    elif raw_pnl > D("0.1"):
                        exit_type = "TP"
                    else:
                        exit_type = "SL"

                closed.append({
                    "time": ts.strftime("%m/%d %H:%M"),
                    "sym": sym, "dir": pos_side, "exit": exit_type,
                    "pnl": float(net_pnl), "fee": float(fee),
                })

                if entry_info:
                    entry_info["notional"] -= notional
                    if entry_info["notional"] <= 0:
                        del opens[k]
                    else:
                        entry_info["fee"] = D("0")
        except Exception as e:
            _log.debug(f"build_closed_positions 解析失敗: {e}")
            continue

    closed.reverse()
    return closed

"""
CRT 策略逆向分析

目標：
1. 找出 TP1/TP2/TP3/TP4 的計算公式
2. 驗證 Entry / SL 來源（信號K線的 close / low）
3. 找出 Sweep 的 lookback 根數
4. 分析 RR 分布與時段規律

使用方式：
    python scripts/crt_reverse_engineer.py
    python scripts/crt_reverse_engineer.py --symbol BTCUSDT.P
    python scripts/crt_reverse_engineer.py --section tp   # 只跑 TP 分析
    python scripts/crt_reverse_engineer.py --section sweep
"""

import sys
import os
import argparse
import logging
from datetime import timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING)

from src.config import load_config
from src.database import init_db, get_session
from src.models import SignalORM, CandleORM, SignalSide

cfg = load_config()
init_db(cfg["database"]["url"])
session = get_session()
symbol_map = cfg.get("market_data", {}).get("symbol_mapping", {})


# ============================================================
# Symbol 轉換：BingX format → CCXT format
# ============================================================

_FOREX_MAP = {
    "NAS100USD": "NQ=F",
    "XAUUSD": "GC=F",
    "XAGUSD": "SI=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "AUDUSD": "AUDUSD=X",
}

def bingx_to_ccxt(symbol: str) -> str:
    """BTCUSDT.P → BTC/USDT，NAS100USD → NQ=F"""
    s = symbol.rstrip(".P")
    if s in _FOREX_MAP:
        return _FOREX_MAP[s]
    # BTCUSDT → BTC/USDT
    if s.endswith("USDT"):
        base = s[:-4]
        return f"{base}/USDT"
    return symbol


# ============================================================
# 工具函式
# ============================================================

def get_candles_before(symbol: str, timeframe: str, signal_time, n: int = 10):
    """取信號K線前 n 根（含信號K線本身）"""
    ccxt_sym = bingx_to_ccxt(symbol)
    rows = (
        session.query(CandleORM)
        .filter(
            CandleORM.symbol == ccxt_sym,
            CandleORM.timeframe == timeframe,
            CandleORM.open_time <= signal_time,
        )
        .order_by(CandleORM.open_time.desc())
        .limit(n)
        .all()
    )
    return list(reversed(rows))  # 時間升序


def load_signals(symbol_filter=None, min_tp4=True):
    q = session.query(SignalORM).filter(SignalORM.entry > 0, SignalORM.sl > 0)
    if symbol_filter:
        q = q.filter(SignalORM.symbol == symbol_filter)
    if min_tp4:
        q = q.filter(SignalORM.tp4 != None)
    return q.order_by(SignalORM.signal_time).all()


# ============================================================
# Section 1：TP 公式分析
# ============================================================

def analyze_tp_formula(signals, sample_size=500):
    """
    兩個假說並列驗證：

    假說 A（C-1 range）：
        tp2 = C-1.high（前一根高點）
        tp1 = (C-1.high + C-1.low) / 2

    假說 B（SL anchor）：
        tp2 = 從 K 線回溯找最近 high > entry（被 sweep 的 liquidity）
        tp1 = (sl + tp2) / 2
        tp3 = tp2 + 0.5 * (tp2 - sl)
        tp4 = tp2 + 1.5 * (tp2 - sl)
    """
    print("\n" + "=" * 60)
    print("  SECTION 1：TP 公式分析")
    print("=" * 60)

    rows = []
    no_candle = 0

    for sig in signals[:sample_size]:
        tf = sig.timeframe or "1h"
        candles = get_candles_before(sig.symbol, tf, sig.signal_time, n=12)
        if len(candles) < 2:
            no_candle += 1
            continue

        c0 = candles[-1]   # 信號K線
        prev = candles[:-1]  # 前面的K線

        # ── 假說 A：TP2 = C-1.high ──
        c1 = prev[-1]
        hyp_a_tp2 = c1.high if sig.side == SignalSide.LONG else c1.low
        hyp_a_tp1 = (c1.high + c1.low) / 2

        # ── 假說 B：TP2 = (sl + tp2) / 2 推導出 tp1；tp3/tp4 從 tp2+sl 延伸 ──
        # 先用實際 tp2 驗 tp1/tp3/tp4 公式
        if sig.tp2:
            hyp_b_tp1 = (sig.sl + sig.tp2) / 2 if sig.side == SignalSide.LONG else (sig.sl + sig.tp2) / 2
            hyp_b_rng = abs(sig.tp2 - sig.sl)
            if sig.side == SignalSide.LONG:
                hyp_b_tp3 = sig.tp2 + 0.5 * hyp_b_rng
                hyp_b_tp4 = sig.tp2 + 1.5 * hyp_b_rng
            else:
                hyp_b_tp3 = sig.tp2 - 0.5 * hyp_b_rng
                hyp_b_tp4 = sig.tp2 - 1.5 * hyp_b_rng
        else:
            hyp_b_tp1 = hyp_b_tp3 = hyp_b_tp4 = None

        # ── 找 tp2 的來源 K 線（哪根 K 線的 high 最接近 tp2）──
        best_lb = None
        best_err = float("inf")
        for i, c in enumerate(reversed(prev)):  # i=0 = C-1
            ref = c.high if sig.side == SignalSide.LONG else c.low
            err = abs(ref - sig.tp2) / sig.entry if sig.tp2 else float("inf")
            if err < best_err:
                best_err = err
                best_lb = i + 1  # 1-indexed lookback

        rows.append({
            "symbol": sig.symbol,
            "side": sig.side.value,
            # 假說 A 誤差
            "a_err_tp1": (sig.tp1 - hyp_a_tp1) if sig.tp1 else None,
            "a_err_tp2": (sig.tp2 - hyp_a_tp2) if sig.tp2 else None,
            # 假說 B 誤差（用實際 tp2 算 tp1/tp3/tp4）
            "b_err_tp1": (sig.tp1 - hyp_b_tp1) if sig.tp1 and hyp_b_tp1 else None,
            "b_err_tp3": (sig.tp3 - hyp_b_tp3) if sig.tp3 and hyp_b_tp3 else None,
            "b_err_tp4": (sig.tp4 - hyp_b_tp4) if sig.tp4 and hyp_b_tp4 else None,
            # tp2 來源 K 線
            "tp2_source_lb": best_lb,
            "tp2_source_err": best_err,
            "entry": sig.entry,
        })

    df = pd.DataFrame(rows)
    print(f"樣本：{len(df)} 筆（跳過 {no_candle} 筆無K線）\n")

    if df.empty:
        print("無資料")
        return

    tol = 0.001  # 0.1% 容差

    # ── 假說 A ──
    print("【假說 A：TP 來自前一根 K 線（C-1）】")
    for col, label in [("a_err_tp1", "TP1"), ("a_err_tp2", "TP2")]:
        err = df[col].dropna()
        ok = (err.abs() / df.loc[err.index, "entry"] < tol).mean()
        print(f"  {label}: 平均誤差={err.mean():+.3f}  命中率(0.1%)={ok:.1%}")

    # ── 假說 B ──
    print("\n【假說 B：TP1=(sl+tp2)/2, TP3=tp2+0.5*range, TP4=tp2+1.5*range】")
    for col, label in [("b_err_tp1", "TP1"), ("b_err_tp3", "TP3"), ("b_err_tp4", "TP4")]:
        err = df[col].dropna()
        ok = (err.abs() / df.loc[err.index, "entry"] < tol).mean()
        print(f"  {label}: 平均誤差={err.mean():+.3f}  命中率(0.1%)={ok:.1%}")

    # ── TP2 來源 K 線分布 ──
    print("\n【TP2 的 K 線來源（哪根 K 線 high 最接近 tp2）】")
    counts = df["tp2_source_lb"].value_counts().sort_index()
    good = df[df["tp2_source_err"] < tol]
    print(f"  誤差 < 0.1% 的比例：{len(good)}/{len(df)} = {len(good)/len(df):.1%}")
    print(f"  Lookback 分布（top 5）：")
    for lb, cnt in counts.head(5).items():
        print(f"    前 {lb:2d} 根：{cnt} ({cnt/len(df):.1%})")

    return df


# ============================================================
# Section 2：Entry / SL 來源驗證
# ============================================================

def analyze_entry_sl(signals, sample_size=500):
    """
    假說：
        entry = C0.close（信號K線收盤）
        sl    = C0.low（信號K線最低點）
    """
    print("\n" + "=" * 60)
    print("  SECTION 2：Entry / SL 來源驗證")
    print("=" * 60)

    entry_match = 0
    sl_match = 0
    total = 0

    for sig in signals[:sample_size]:
        tf = sig.timeframe or "1h"
        candles = get_candles_before(sig.symbol, tf, sig.signal_time, n=3)
        if not candles:
            continue
        c0 = candles[-1]
        total += 1

        tol = sig.entry * 0.0005  # 0.05% 容差
        if abs(sig.entry - c0.close) < tol:
            entry_match += 1
        if abs(sig.sl - c0.low) < tol:
            sl_match += 1

    if total == 0:
        print("無 K 線資料")
        return

    print(f"樣本：{total} 筆")
    print(f"entry = C0.close：{entry_match}/{total} = {entry_match/total:.1%}")
    print(f"sl    = C0.low  ：{sl_match}/{total} = {sl_match/total:.1%}")

    # SHORT 方向
    short_entry = 0
    short_sl = 0
    short_total = 0
    for sig in [s for s in signals if s.side == SignalSide.SHORT][:200]:
        tf = sig.timeframe or "1h"
        candles = get_candles_before(sig.symbol, tf, sig.signal_time, n=3)
        if not candles:
            continue
        c0 = candles[-1]
        short_total += 1
        tol = sig.entry * 0.0005
        if abs(sig.entry - c0.close) < tol:
            short_entry += 1
        if abs(sig.sl - c0.high) < tol:
            short_sl += 1

    if short_total:
        print(f"\nSHORT 方向（{short_total} 筆）：")
        print(f"entry = C0.close：{short_entry}/{short_total} = {short_entry/short_total:.1%}")
        print(f"sl    = C0.high ：{short_sl}/{short_total} = {short_sl/short_total:.1%}")


# ============================================================
# Section 3：Sweep Lookback 分析
# ============================================================

def analyze_sweep_lookback(signals, sample_size=400):
    """
    假說：信號K線的 low（LONG）必須 < 前 N 根的最低 low
    找出 N 的分布
    """
    print("\n" + "=" * 60)
    print("  SECTION 3：Sweep Lookback 分析")
    print("=" * 60)

    long_sigs = [s for s in signals if s.side == SignalSide.LONG][:sample_size]
    lookback_counts = defaultdict(int)
    no_sweep_count = 0
    no_candle = 0

    for sig in long_sigs:
        tf = sig.timeframe or "1h"
        candles = get_candles_before(sig.symbol, tf, sig.signal_time, n=12)
        if len(candles) < 3:
            no_candle += 1
            continue

        c0 = candles[-1]  # 信號K線
        prev = candles[:-1]  # 前幾根

        swept = False
        for lookback in range(1, len(prev) + 1):
            target = prev[-lookback]
            if c0.low < target.low:
                lookback_counts[lookback] += 1
                swept = True
                break  # 找到最近的被 sweep 的那根就停

        if not swept:
            no_sweep_count += 1

    total = len(long_sigs) - no_candle
    print(f"LONG 樣本：{len(long_sigs)} 筆，有K線：{total} 筆")
    print(f"找不到 sweep target：{no_sweep_count} 筆 ({no_sweep_count/total:.1%})\n")
    print("Sweep lookback 分布（掃到前第幾根）：")
    for lb in sorted(lookback_counts):
        pct = lookback_counts[lb] / total
        bar = "█" * int(pct * 40)
        print(f"  前 {lb:2d} 根：{lookback_counts[lb]:4d} ({pct:.1%})  {bar}")

    # SHORT 方向
    short_sigs = [s for s in signals if s.side == SignalSide.SHORT][:sample_size]
    short_counts = defaultdict(int)
    short_no_sweep = 0
    short_no_candle = 0

    for sig in short_sigs:
        tf = sig.timeframe or "1h"
        candles = get_candles_before(sig.symbol, tf, sig.signal_time, n=12)
        if len(candles) < 3:
            short_no_candle += 1
            continue
        c0 = candles[-1]
        prev = candles[:-1]
        swept = False
        for lookback in range(1, len(prev) + 1):
            target = prev[-lookback]
            if c0.high > target.high:
                short_counts[lookback] += 1
                swept = True
                break
        if not swept:
            short_no_sweep += 1

    short_total = len(short_sigs) - short_no_candle
    print(f"\nSHORT 樣本：{len(short_sigs)} 筆，有K線：{short_total} 筆")
    print(f"找不到 sweep target：{short_no_sweep} 筆 ({short_no_sweep/short_total:.1%})\n")
    print("Sweep lookback 分布（掃到前第幾根）：")
    for lb in sorted(short_counts):
        pct = short_counts[lb] / short_total
        bar = "█" * int(pct * 40)
        print(f"  前 {lb:2d} 根：{short_counts[lb]:4d} ({pct:.1%})  {bar}")


# ============================================================
# Section 4：RR 分布與信號時段
# ============================================================

def analyze_signal_patterns(signals):
    print("\n" + "=" * 60)
    print("  SECTION 4：信號特性分析")
    print("=" * 60)

    rows = []
    for sig in signals:
        if not sig.tp2 or not sig.sl or sig.entry <= 0:
            continue
        risk = abs(sig.entry - sig.sl)
        if risk <= 0:
            continue
        tp2_rr = abs(sig.tp2 - sig.entry) / risk
        tp4_rr = abs(sig.tp4 - sig.entry) / risk if sig.tp4 else None
        rows.append({
            "symbol": sig.symbol,
            "side": sig.side.value,
            "tf": sig.timeframe,
            "hour_utc": sig.signal_time.hour,
            "weekday": sig.signal_time.weekday(),
            "tp2_rr": tp2_rr,
            "tp4_rr": tp4_rr,
            "sl_pct": risk / sig.entry,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return

    print(f"\n總信號：{len(df)}")
    print(f"TP2 RR：中位={df['tp2_rr'].median():.2f}  平均={df['tp2_rr'].mean():.2f}")
    if df["tp4_rr"].notna().any():
        print(f"TP4 RR：中位={df['tp4_rr'].median():.2f}  平均={df['tp4_rr'].mean():.2f}")
    print(f"SL 距離：中位={df['sl_pct'].median():.3%}  平均={df['sl_pct'].mean():.3%}")

    # Timeframe 分布
    print(f"\nTimeframe 分布：")
    for tf, cnt in df["tf"].value_counts().items():
        print(f"  {tf}: {cnt} ({cnt/len(df):.1%})")

    # 每小時信號數（UTC）
    print(f"\n時段分布（UTC，信號最多的 5 個小時）：")
    for hr, cnt in df["hour_utc"].value_counts().head(5).items():
        print(f"  {hr:02d}:00 UTC：{cnt} 筆")

    # TP2 RR 分布（低RR vs 高RR 門檻）
    print(f"\nTP2 RR 分布：")
    for threshold in [0.5, 1.0, 1.5, 2.0, 3.0]:
        pct = (df["tp2_rr"] >= threshold).mean()
        print(f"  RR >= {threshold:.1f}：{pct:.1%}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--section", default="all", choices=["all", "tp", "entry", "sweep", "pattern"])
    parser.add_argument("--sample", type=int, default=500)
    args = parser.parse_args()

    print(f"載入信號（symbol={args.symbol or 'all'}）...")
    signals = load_signals(symbol_filter=args.symbol, min_tp4=True)
    print(f"共 {len(signals)} 筆有 TP4 的完整信號")

    if args.section in ("all", "tp"):
        analyze_tp_formula(signals, args.sample)

    if args.section in ("all", "entry"):
        analyze_entry_sl(signals, args.sample)

    if args.section in ("all", "sweep"):
        analyze_sweep_lookback(signals, args.sample)

    if args.section in ("all", "pattern"):
        analyze_signal_patterns(signals)

    session.close()


if __name__ == "__main__":
    main()

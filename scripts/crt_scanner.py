"""
CRT 自主掃描器

根據逆向工程結果，從 K 線自行掃描 CRT 信號，不依賴 Telegram 頻道。

逆向工程結果（4413 筆歷史信號，Recall 78.5%，TP 誤差 < 0.5% 覆蓋率 91~95%）：

CRT 三步結構：
  C-1 = Swept candle（被 sweep 的 K 線，同時也是 range candle）
  C0  = Signal candle（sweep 並 reclaim 的 K 線）

LONG：
  條件：C0.low < C-1.low  且  C0.close > C-1.low
  Entry = C0.close
  SL    = C0.low
  TP2   = C-1.high
  TP1   = SL + 0.50 * (TP2 - SL)
  TP3   = TP2 + 0.42 * (TP2 - SL)
  TP4   = TP2 + 1.25 * (TP2 - SL)

SHORT（對稱）：
  條件：C0.high > C-1.high  且  C0.close < C-1.high
  Entry = C0.close
  SL    = C0.high
  TP2   = C-1.low
  TP1   = SL - 0.50 * (SL - TP2)
  TP3   = TP2 - 0.42 * (SL - TP2)
  TP4   = TP2 - 1.25 * (SL - TP2)

Sweep 分布：90.8% 掃 C-1，9.2% 掃 C-2

使用方式：
  python scripts/crt_scanner.py --mode validate
  python scripts/crt_scanner.py --mode validate --symbol BTCUSDT.P
  python scripts/crt_scanner.py --mode live --symbols BTCUSDT.P ETHUSDT.P SOLUSDT.P
"""

import sys
import os
import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import ccxt
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("crt_scanner")


# ============================================================
# 參數設定
# ============================================================

@dataclass
class ScannerConfig:
    # Sweep lookback：掃前幾根（依序嘗試）
    # 最佳：[1, 2]（78.5% recall，grid search 結果）
    sweep_lookbacks: list = field(default_factory=lambda: [1, 2])
    # Range candle 相對 sweep candle 的偏移
    # 0 = 被 sweep 的 K 線本身就是 range candle（TP2 = sweep_c.high/low）
    range_offset: int = 0
    # TP 公式係數（逆向工程結果）
    tp1_ratio: float = 0.50    # SL + ratio * (TP2 - SL)
    tp3_mult:  float = 0.42    # TP2 + mult * (TP2 - SL)
    tp4_mult:  float = 1.25    # TP2 + mult * (TP2 - SL)
    # 過濾
    min_rr:    float = 0.5     # 最低 TP2 RR
    min_sl_pct: float = 0.001  # 最低 SL 距離（0.1%）
    max_sl_pct: float = 0.05   # 最大 SL 距離（5%）
    # 不允許 C0 突破 sweep candle 的高點
    require_no_high_break: bool = False


# ============================================================
# CRT 信號 dataclass
# ============================================================

@dataclass
class CRTSignal:
    symbol: str
    side: str          # "long" / "short"
    timeframe: str
    signal_time: datetime
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    tp4: float
    rr_at_tp2: float
    sweep_lb: int      # 掃到前第幾根
    range_lb: int      # range candle 在前第幾根


# ============================================================
# 核心偵測函式
# ============================================================

def detect_crt(candles: list[dict], symbol: str, timeframe: str,
               cfg: ScannerConfig = ScannerConfig()) -> Optional[CRTSignal]:
    """
    candles: 時間升序的 OHLCV dict 列表，至少需要 cfg.sweep_lookbacks[-1]+cfg.range_offset+2 根
    回傳第一個符合條件的 CRT 信號，否則回傳 None
    """
    if len(candles) < 5:
        return None

    c0 = candles[-1]
    entry = c0["close"]

    # ── LONG 掃描 ──
    for sweep_lb in cfg.sweep_lookbacks:
        idx_sweep = -(sweep_lb + 1)
        idx_range = -(sweep_lb + cfg.range_offset + 1)

        if abs(idx_sweep) > len(candles) or abs(idx_range) > len(candles):
            continue

        sweep_c = candles[idx_sweep]
        range_c = candles[idx_range]

        # Sweep 條件
        if c0["low"] >= sweep_c["low"]:
            continue
        # Reclaim 條件
        if c0["close"] <= sweep_c["low"]:
            continue
        # No high break（選擇性）
        if cfg.require_no_high_break and c0["high"] >= sweep_c["high"]:
            continue

        # TP2 = range_c.high
        tp2 = range_c["high"]
        sl = c0["low"]

        # tp2 必須在 entry 上方
        if tp2 <= entry:
            # fallback：用 sweep candle 的 high
            tp2 = sweep_c["high"]
            if tp2 <= entry:
                continue

        # SL 距離過濾
        sl_pct = (entry - sl) / entry
        if sl_pct < cfg.min_sl_pct or sl_pct > cfg.max_sl_pct:
            continue

        # TP 計算
        rng = tp2 - sl
        tp1 = sl + cfg.tp1_ratio * rng
        tp3 = tp2 + cfg.tp3_mult * rng
        tp4 = tp2 + cfg.tp4_mult * rng

        # RR 過濾
        rr = (tp2 - entry) / (entry - sl)
        if rr < cfg.min_rr:
            continue

        sig_time = c0.get("time") or datetime.now(timezone.utc)

        return CRTSignal(
            symbol=symbol, side="long", timeframe=timeframe,
            signal_time=sig_time, entry=entry, sl=sl,
            tp1=round(tp1, 6), tp2=round(tp2, 6),
            tp3=round(tp3, 6), tp4=round(tp4, 6),
            rr_at_tp2=round(rr, 3), sweep_lb=sweep_lb,
            range_lb=sweep_lb + cfg.range_offset,
        )

    # ── SHORT 掃描 ──
    for sweep_lb in cfg.sweep_lookbacks:
        idx_sweep = -(sweep_lb + 1)
        idx_range = -(sweep_lb + cfg.range_offset + 1)

        if abs(idx_sweep) > len(candles) or abs(idx_range) > len(candles):
            continue

        sweep_c = candles[idx_sweep]
        range_c = candles[idx_range]

        if c0["high"] <= sweep_c["high"]:
            continue
        if c0["close"] >= sweep_c["high"]:
            continue
        if cfg.require_no_high_break and c0["low"] <= sweep_c["low"]:
            continue

        tp2 = range_c["low"]
        sl = c0["high"]

        if tp2 >= entry:
            tp2 = sweep_c["low"]
            if tp2 >= entry:
                continue

        sl_pct = (sl - entry) / entry
        if sl_pct < cfg.min_sl_pct or sl_pct > cfg.max_sl_pct:
            continue

        rng = sl - tp2
        tp1 = sl - cfg.tp1_ratio * rng
        tp3 = tp2 - cfg.tp3_mult * rng
        tp4 = tp2 - cfg.tp4_mult * rng

        rr = (entry - tp2) / (sl - entry)
        if rr < cfg.min_rr:
            continue

        sig_time = c0.get("time") or datetime.now(timezone.utc)

        return CRTSignal(
            symbol=symbol, side="short", timeframe=timeframe,
            signal_time=sig_time, entry=entry, sl=sl,
            tp1=round(tp1, 6), tp2=round(tp2, 6),
            tp3=round(tp3, 6), tp4=round(tp4, 6),
            rr_at_tp2=round(rr, 3), sweep_lb=sweep_lb,
            range_lb=sweep_lb + cfg.range_offset,
        )

    return None


# ============================================================
# K 線抓取（CCXT）
# ============================================================

def fetch_candles_ccxt(symbol: str, timeframe: str, since: datetime,
                       until: datetime, exchange=None) -> list[dict]:
    """用 ccxt 抓取 OHLCV，轉換成 dict 列表"""
    if exchange is None:
        exchange = ccxt.okx({"enableRateLimit": True})

    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)
    all_bars = []
    limit = 200

    while True:
        bars = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        if not bars:
            break
        for b in bars:
            if b[0] < until_ms:
                all_bars.append({
                    "time": datetime.fromtimestamp(b[0] / 1000, tz=timezone.utc),
                    "open": b[1], "high": b[2], "low": b[3],
                    "close": b[4], "volume": b[5],
                })
        since_ms = bars[-1][0] + 1
        if len(bars) < limit or bars[-1][0] >= until_ms:
            break

    return all_bars


# ============================================================
# Validation Mode
# ============================================================

def bingx_to_ccxt_symbol(symbol: str) -> str:
    s = symbol.rstrip(".P")
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    forex = {
        "NAS100USD": "NQ=F", "XAUUSD": "GC=F", "XAGUSD": "SI=F",
        "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X",
        "USDJPY": "USDJPY=X", "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X", "AUDUSD": "AUDUSD=X",
    }
    return forex.get(s, symbol)


def validate(symbol_filter: str | None = None, cfg: ScannerConfig = ScannerConfig(),
             max_signals: int = 500, verbose: bool = False):
    """
    對每筆 DB 信號，用對應時段的 K 線跑掃描器，比對結果
    輸出：Recall / Precision / TP 誤差
    """
    from src.config import load_config
    from src.database import init_db, get_session
    from src.models import SignalORM, CandleORM, SignalSide

    config = load_config()
    init_db(config["database"]["url"])
    session = get_session()

    q = session.query(SignalORM).filter(
        SignalORM.entry > 0, SignalORM.sl > 0, SignalORM.tp2 != None,
    )
    if symbol_filter:
        q = q.filter(SignalORM.symbol == symbol_filter)
    db_signals = q.order_by(SignalORM.signal_time).limit(max_signals).all()

    print(f"\n驗證 {len(db_signals)} 筆信號（symbol={symbol_filter or 'all'}）...\n")

    matched = 0
    total = 0
    side_wrong = 0
    not_found = 0
    tp2_errors = []
    tp1_errors = []
    tp3_errors = []
    tp4_errors = []
    sweep_lb_dist = {}

    for sig in db_signals:
        tf = sig.timeframe or "1h"
        ccxt_sym = bingx_to_ccxt_symbol(sig.symbol)

        # 對齊到 K 線邊界，避免把剛開盤的不完整 K 線當 C0
        st = sig.signal_time
        if tf in ("1h", "1H"):
            # 最後完整的 1H K 線開盤時間 = signal_time 的整點（如 14:02 → 13:00）
            cutoff = st.replace(minute=0, second=0, microsecond=0)
        elif tf in ("4h", "4H"):
            cutoff = st.replace(minute=0, second=0, microsecond=0)
            cutoff = cutoff - timedelta(hours=cutoff.hour % 4)
        else:
            cutoff = st

        # 從 DB 撈 K 線（只取完整 K 線：open_time < cutoff，即 cutoff 整點之前）
        rows = (
            session.query(CandleORM)
            .filter(
                CandleORM.symbol == ccxt_sym,
                CandleORM.timeframe == tf,
                CandleORM.open_time < cutoff,
                CandleORM.open_time >= cutoff - timedelta(hours=30),
            )
            .order_by(CandleORM.open_time)
            .all()
        )

        if len(rows) < 5:
            not_found += 1
            continue

        candles = [
            {"time": r.open_time, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume or 0}
            for r in rows
        ]

        total += 1
        result = detect_crt(candles, ccxt_sym, tf, cfg)

        if result is None:
            if verbose:
                print(f"  MISS  {sig.symbol} {sig.side.value} @ {sig.signal_time:%m-%d %H:%M}")
            continue

        # 方向比對
        db_side = sig.side.value
        if result.side != db_side:
            side_wrong += 1
            if verbose:
                print(f"  SIDE  {sig.symbol} db={db_side} scan={result.side} @ {sig.signal_time:%m-%d %H:%M}")
            continue

        matched += 1

        # TP 誤差（相對 entry 的百分比）
        base = sig.entry
        if sig.tp2:
            tp2_errors.append(abs(result.tp2 - sig.tp2) / base)
        if sig.tp1:
            tp1_errors.append(abs(result.tp1 - sig.tp1) / base)
        if sig.tp3:
            tp3_errors.append(abs(result.tp3 - sig.tp3) / base)
        if sig.tp4:
            tp4_errors.append(abs(result.tp4 - sig.tp4) / base)

        lb = result.sweep_lb
        sweep_lb_dist[lb] = sweep_lb_dist.get(lb, 0) + 1

    session.close()

    recall = matched / total if total else 0
    print("=" * 55)
    print(f"  Recall : {matched}/{total} = {recall:.1%}  (無K線 {not_found} 筆, 方向錯 {side_wrong} 筆)")
    print("=" * 55)

    def pct_err(errs, label):
        if not errs:
            return
        arr = np.array(errs)
        print(f"  {label} 誤差：中位 {np.median(arr):.3%}  <0.5% {(arr<0.005).mean():.1%}  <1% {(arr<0.01).mean():.1%}")

    print("\nTP 誤差（相對 entry）：")
    pct_err(tp2_errors, "TP2")
    pct_err(tp1_errors, "TP1")
    pct_err(tp3_errors, "TP3")
    pct_err(tp4_errors, "TP4")

    print("\nSweep lookback 分布（掃到前第幾根）：")
    for lb in sorted(sweep_lb_dist):
        cnt = sweep_lb_dist[lb]
        print(f"  前 {lb} 根：{cnt} ({cnt/matched:.1%})")

    return recall


# ============================================================
# Grid Search：自動找最佳參數組合
# ============================================================

def grid_search(symbol_filter: str | None = None, max_signals: int = 800):
    print("\n=== Grid Search ===\n")

    best_recall = 0
    best_cfg = None
    results = []

    param_grid = {
        "sweep_lookbacks": [[1, 2, 3, 4, 5], [1, 2, 3], [1, 2]],
        "range_offset":    [0, 1, 2],
        "min_rr":          [0.5, 1.0],
        "require_no_high_break": [True, False],
    }

    from itertools import product
    keys = list(param_grid.keys())
    combos = list(product(*param_grid.values()))
    print(f"共 {len(combos)} 組參數，每組驗證 {max_signals} 筆信號...\n")

    for i, vals in enumerate(combos):
        params = dict(zip(keys, vals))
        cfg = ScannerConfig(**params)
        # 靜默跑驗證
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            recall = validate(symbol_filter=symbol_filter, cfg=cfg,
                              max_signals=max_signals, verbose=False)
        results.append((recall, params))
        if recall > best_recall:
            best_recall = recall
            best_cfg = params
        if (i + 1) % 4 == 0:
            print(f"  進度 {i+1}/{len(combos)}，目前最佳 recall={best_recall:.1%}")

    results.sort(key=lambda x: -x[0])
    print(f"\n最佳參數（recall={best_recall:.1%}）：")
    for k, v in best_cfg.items():
        print(f"  {k} = {v}")
    print("\nTop 5：")
    for recall, params in results[:5]:
        print(f"  {recall:.1%}  {params}")


# ============================================================
# Live Mode
# ============================================================

def live_scan(symbols: list[str], timeframe: str = "1h",
              cfg: ScannerConfig = ScannerConfig()):
    import time as _time

    exchange = ccxt.okx({"enableRateLimit": True})
    print(f"Live scan 啟動：{symbols}  tf={timeframe}\n按 Ctrl+C 停止\n")

    seen = set()
    while True:
        for sym in symbols:
            try:
                bars = exchange.fetch_ohlcv(sym, timeframe, limit=20)
                candles = [
                    {"time": datetime.fromtimestamp(b[0]/1000, tz=timezone.utc),
                     "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]}
                    for b in bars
                ]
                result = detect_crt(candles, sym, timeframe, cfg)
                if result:
                    key = (sym, result.side, result.signal_time)
                    if key not in seen:
                        seen.add(key)
                        print(f"[{datetime.now():%H:%M:%S}] {result.symbol} {result.side.upper()} "
                              f"entry={result.entry:.4f} sl={result.sl:.4f} "
                              f"tp2={result.tp2:.4f} RR={result.rr_at_tp2:.2f}")
            except Exception as e:
                logger.warning(f"{sym}: {e}")
        _time.sleep(30)


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="validate", choices=["validate", "live", "grid"])
    parser.add_argument("--symbol", default=None, help="單一幣種過濾（validate 用）")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
                        help="Live 模式掃描標的")
    parser.add_argument("--tf", default="1h")
    parser.add_argument("--max", type=int, default=800, help="驗證最多 N 筆")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cfg = ScannerConfig()

    if args.mode == "validate":
        validate(symbol_filter=args.symbol, cfg=cfg,
                 max_signals=args.max, verbose=args.verbose)

    elif args.mode == "grid":
        grid_search(symbol_filter=args.symbol, max_signals=args.max)

    elif args.mode == "live":
        live_scan(args.symbols, timeframe=args.tf, cfg=cfg)


if __name__ == "__main__":
    main()

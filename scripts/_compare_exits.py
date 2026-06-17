"""對比 0/0/50/50 vs 0/20/30/50 — 分別跑加密和外匯"""
import sys, os, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING)

from src.config import load_config
from src.database import init_db, get_session
from src.models import BacktestConfig, AmbiguousMode, SignalORM, TradeResult
from src.backtest.engine import simulate_trade
from src.market_data.provider import load_candles
from datetime import timedelta

cfg = load_config()
init_db(cfg["database"]["url"])
symbol_map = cfg.get("market_data", {}).get("symbol_mapping", {})
s = get_session()


def run_group(group_name, source_filter):
    """跑一組信號（4/1 之後）"""
    from datetime import datetime as _dt
    cutoff = _dt(2026, 4, 1)
    sigs = s.query(SignalORM).filter(
        SignalORM.source.in_(source_filter),
        SignalORM.entry > 0,
        SignalORM.sl > 0,
        SignalORM.signal_time >= cutoff,
    ).order_by(SignalORM.signal_time).all()

    if not sigs:
        print(f"{group_name}: no signals")
        return

    print(f"\n=== {group_name} ({len(sigs)} signals) ===")

    configs = {
        "0/0/50/50":   {"tp1": 0, "tp2": 0, "tp3": 0.5, "tp4": 0.5},
        "0/20/30/50":  {"tp1": 0, "tp2": 0.2, "tp3": 0.3, "tp4": 0.5},
    }

    # K 線快取
    candle_cache = {}
    def get_candles(sig):
        key = (sig.symbol, sig.signal_time)
        if key not in candle_cache:
            try:
                candle_cache[key] = load_candles(
                    s, sig.symbol, sig.timeframe or "1h",
                    sig.signal_time, sig.signal_time + timedelta(hours=168),
                    symbol_map=symbol_map,
                )
            except Exception:
                candle_cache[key] = None
        return candle_cache[key]

    for name, weights in configs.items():
        bt = BacktestConfig(
            name="t", mode="partial_be", target_tp="tp4",
            partial_weights=weights, move_sl_after="tp1",
            ambiguous_mode=AmbiguousMode.CONSERVATIVE,
            signal_expiry_bars=48, signal_expiry_hours=72,
        )
        results = []
        for sig in sigs:
            cs = get_candles(sig)
            if not cs:
                results.append(TradeResult(signal_id=sig.id, triggered=False))
                continue
            results.append(simulate_trade(sig, cs, bt))

        triggered = [r for r in results if r.triggered]
        n = len(triggered)
        if not n:
            print(f"  {name}: no triggered")
            continue

        wins = sum(1 for r in triggered if (r.pnl_r or 0) > 0)
        losses = n - wins
        total_r = sum(r.pnl_r or 0 for r in triggered)
        avg_w = sum(r.pnl_r for r in triggered if (r.pnl_r or 0) > 0) / wins if wins else 0
        avg_l = sum(abs(r.pnl_r) for r in triggered if r.pnl_r is not None and (r.pnl_r or 0) <= 0) / losses if losses else 0
        wr = wins / n * 100
        ev = total_r / n
        rr = avg_w / avg_l if avg_l else 0

        print(
            f"  {name:>12}: WR={wr:>5.1f}%  "
            f"平均盈={avg_w:>+6.3f}R  平均虧={-avg_l:>+6.3f}R  "
            f"RR={rr:>4.2f}  EV={ev:>+7.4f}R  累積={total_r:>+7.1f}R"
        )


# 加密：CRT_SNIPER_CRYPTO + 即時 channel
run_group("加密貨幣", ["CRT_SNIPER_CRYPTO", "-1003868559200"])
# 外匯：CRT_SNIPER_CFD + CFD channel
run_group("外匯/CFD", ["CRT_SNIPER_CFD", "-1003706518531"])

s.close()

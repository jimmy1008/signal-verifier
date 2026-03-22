"""
腳本：執行回測並輸出統計

使用方式：
    python scripts/run_backtest.py
    python scripts/run_backtest.py --mode single_tp --target tp2
    python scripts/run_backtest.py --mode breakeven
    python scripts/run_backtest.py --export csv

流程：
    1. 從 DB 讀取已解析的信號
    2. 載入 K 線
    3. 模擬交易
    4. 計算績效指標
    5. 輸出報表
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.database import init_db, get_session
from src.models import BacktestConfig, AmbiguousMode
from src.backtest.runner import run_backtest
from src.stats.metrics import compute_metrics, build_equity_curve, export_csv, export_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="執行信號回測")
    parser.add_argument("--mode", default="single_tp", choices=["single_tp", "partial_tp", "breakeven"])
    parser.add_argument("--target", default="tp2", help="single_tp 模式的目標 TP (tp1-tp4)")
    parser.add_argument("--ambiguous", default="conservative", choices=["conservative", "optimistic"])
    parser.add_argument("--source", default=None, help="篩選特定來源")
    parser.add_argument("--export", default=None, choices=["csv", "json", "both"])
    parser.add_argument("--output-dir", default="output", help="報表輸出目錄")
    args = parser.parse_args()

    config_data = load_config()
    init_db(config_data["database"]["url"])

    bt_config = BacktestConfig(
        name=f"{args.mode}_{args.target}_{args.ambiguous}",
        mode=args.mode,
        target_tp=args.target,
        ambiguous_mode=AmbiguousMode(args.ambiguous),
    )

    # 從 YAML 覆寫
    yaml_bt = config_data.get("backtest", {})
    if yaml_bt.get("signal_expiry", {}).get("enabled"):
        bt_config.signal_expiry_bars = yaml_bt["signal_expiry"].get("max_bars", 48)
        bt_config.signal_expiry_hours = yaml_bt["signal_expiry"].get("max_hours", 72)

    symbol_map = config_data.get("market_data", {}).get("symbol_mapping", {})

    session = get_session()
    try:
        results = run_backtest(
            session, bt_config,
            symbol_map=symbol_map,
            source=args.source,
        )
    finally:
        session.close()

    # 計算統計
    metrics = compute_metrics(results)

    # 輸出
    print("\n" + "=" * 60)
    print("  回測結果")
    print("=" * 60)
    print(f"  模式:         {bt_config.mode} (target: {bt_config.target_tp})")
    print(f"  衝突處理:     {bt_config.ambiguous_mode.value}")
    print(f"  總信號數:     {metrics.total_signals}")
    print(f"  觸發數:       {metrics.triggered_count}")
    print(f"  未觸發:       {metrics.not_triggered_count}")
    print(f"  ──────────────────────────────────")
    print(f"  勝率:         {metrics.win_rate:.1%}")
    print(f"  敗率:         {metrics.loss_rate:.1%}")
    print(f"  平均盈利 R:   {metrics.avg_win_r:+.4f}")
    print(f"  平均虧損 R:   {metrics.avg_loss_r:+.4f}")
    print(f"  平均 RR:      {metrics.avg_rr:.2f}")
    print(f"  期望值:       {metrics.expectancy:+.4f} R")
    print(f"  ──────────────────────────────────")
    print(f"  累積 R:       {metrics.total_r:+.2f}")
    print(f"  最大回撤:     {metrics.max_drawdown_r:.2f} R")
    print(f"  最長連勝:     {metrics.max_consecutive_wins}")
    print(f"  最長連敗:     {metrics.max_consecutive_losses}")
    print(f"  ──────────────────────────────────")
    print(f"  TP1 觸達率:   {metrics.tp1_hit_rate:.1%}")
    print(f"  TP2 觸達率:   {metrics.tp2_hit_rate:.1%}")
    print(f"  TP3 觸達率:   {metrics.tp3_hit_rate:.1%}")
    print(f"  TP4 觸達率:   {metrics.tp4_hit_rate:.1%}")
    print(f"  碰TP1後打SL:  {metrics.tp1_hit_then_sl_rate:.1%}")
    print("=" * 60)

    # 匯出
    if args.export:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(exist_ok=True)

        if args.export in ("csv", "both"):
            export_csv(results, str(output_dir / f"results_{bt_config.name}.csv"))
        if args.export in ("json", "both"):
            export_json(results, str(output_dir / f"results_{bt_config.name}.json"))

        # Equity curve
        eq = build_equity_curve(results)
        if not eq.empty:
            eq.to_csv(str(output_dir / f"equity_{bt_config.name}.csv"), index=False)
            print(f"\n報表已匯出至 {output_dir}/")


if __name__ == "__main__":
    main()

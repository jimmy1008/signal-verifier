"""
腳本：執行完整 Edge 判定

使用方式：
    python scripts/evaluate.py
    python scripts/evaluate.py --source signal_channel_1
    python scripts/evaluate.py --no-stability
    python scripts/evaluate.py --latency
    python scripts/evaluate.py --time-analysis
    python scripts/evaluate.py --full

流程：
    1. 基礎回測 + 指標計算
    2. Edge 判定
    3. (可選) 策略穩定性測試
    4. (可選) 延遲敏感度測試
    5. (可選) 時間切片分析
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.database import init_db, get_session
from src.models import BacktestConfig, SignalORM
from src.backtest.runner import run_backtest
from src.stats.metrics import compute_metrics
from src.evaluator.judge import evaluate_edge, run_stability_test, full_evaluation
from src.backtest.latency_test import run_latency_test
from src.stats.time_analysis import analyze_by_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="完整 Edge 判定")
    parser.add_argument("--source", default=None)
    parser.add_argument("--no-stability", action="store_true", help="跳過穩定性測試")
    parser.add_argument("--latency", action="store_true", help="執行延遲敏感度測試")
    parser.add_argument("--time-analysis", action="store_true", help="執行時間切片分析")
    parser.add_argument("--full", action="store_true", help="全部都跑")
    parser.add_argument("--output", default=None, help="輸出 JSON 路徑")
    args = parser.parse_args()

    config_data = load_config()
    init_db(config_data["database"]["url"])
    symbol_map = config_data.get("market_data", {}).get("symbol_mapping", {})

    session = get_session()

    try:
        # ── 1. 基礎 Edge 判定 ──
        print("\n" + "=" * 60)
        print("  EDGE EVALUATION")
        print("=" * 60)

        run_stab = not args.no_stability or args.full
        verdict = full_evaluation(session, source=args.source, symbol_map=symbol_map, run_stability=run_stab)

        # 輸出判定
        edge_status = "HAS EDGE" if verdict.has_edge else "NO EDGE"
        trade_status = "TRADEABLE" if verdict.tradeable else "NOT TRADEABLE"
        print(f"\n  >>> {edge_status} | {trade_status} (confidence: {verdict.confidence:.0%}) <<<\n")

        if verdict.reasons:
            print("  Reasons:")
            for r in verdict.reasons:
                print(f"    - {r}")

        if verdict.warnings:
            print("\n  Warnings:")
            for w in verdict.warnings:
                print(f"    ! {w}")

        # 資金模擬結果
        if verdict.capital:
            cap = verdict.capital
            print(f"\n  Capital Simulation (1% risk):")
            print(f"    Final capital:     ${cap.final_capital:.2f}")
            print(f"    Total return:      {cap.total_return_pct:+.1%}")
            print(f"    Max drawdown:      {cap.max_drawdown_pct:.1%}")
            print(f"    Max losing streak: {cap.max_losing_streak}")
            print(f"    Capital floor:     {cap.min_capital_ratio:.0%} of initial")
            if cap.recovery_trades is not None:
                print(f"    Recovery trades:   {cap.recovery_trades}")
            print(f"    Verdict:           {cap.verdict.upper()}")
            for r in cap.reasons:
                print(f"      {r}")

        # 穩定性結果
        if verdict.stability:
            print("\n  Strategy Stability:")
            print(f"    Unstable: {verdict.stability.unstable_strategy}")
            print(f"    Profitable modes: {verdict.stability.profitable_modes}/{verdict.stability.total_modes}")
            for mode, r_val in verdict.stability.mode_results.items():
                marker = "+" if r_val > 0 else "-"
                print(f"    [{marker}] {mode}: {r_val:+.2f}R")

        # ── 2. 延遲測試 ──
        if args.latency or args.full:
            print("\n" + "-" * 60)
            print("  LATENCY SENSITIVITY TEST")
            print("-" * 60)

            bt_config = BacktestConfig(name="latency_test", mode="single_tp", target_tp="tp2")
            latency = run_latency_test(session, bt_config, source=args.source, symbol_map=symbol_map)

            status = "SENSITIVE" if latency.latency_sensitive else "ROBUST"
            print(f"\n  >>> {status} <<<")
            print(f"  Max viable delay: {latency.max_viable_delay}")
            print(f"  Degradation: {latency.degradation_pct:.0f}%\n")

            for reason in latency.reasons:
                print(f"    {reason}")

        # ── 3. 時間切片分析 ──
        if args.time_analysis or args.full:
            print("\n" + "-" * 60)
            print("  TIME SESSION ANALYSIS")
            print("-" * 60)

            bt_config = BacktestConfig(name="time_analysis", mode="single_tp", target_tp="tp2")
            results = run_backtest(session, bt_config, source=args.source, symbol_map=symbol_map)

            signals = {s.id: s for s in session.query(SignalORM).all()}
            time_result = analyze_by_session(results, signals)

            stable_str = "STABLE" if time_result.edge_stable else "UNSTABLE"
            print(f"\n  Edge across sessions: {stable_str}")
            print(f"  Distribution: {time_result.edge_distribution}")

            if time_result.best_session:
                print(f"  Best session:  {time_result.best_session}")
            if time_result.worst_session:
                print(f"  Worst session: {time_result.worst_session}")

            for reason in time_result.reasons:
                print(f"    {reason}")

        print("\n" + "=" * 60)

        # ── 輸出 JSON ──
        if args.output:
            output = {
                "has_edge": verdict.has_edge,
                "confidence": verdict.confidence,
                "reasons": verdict.reasons,
                "warnings": verdict.warnings,
                "details": verdict.details,
            }
            if verdict.stability:
                output["stability"] = verdict.stability.model_dump()

            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            print(f"\n  判定結果已匯出: {args.output}")

    finally:
        session.close()


if __name__ == "__main__":
    main()

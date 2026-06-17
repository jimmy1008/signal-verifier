"""從 log + K 線重建 paper 模式的完整交易歷史"""
import sys, re, json, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8")

from src.config import load_config
from src.database import init_db, get_session
from src.market_data.provider import load_candles
from datetime import datetime, timedelta

PAPER_START = "2026-03-29 21:56"

# Step 1: Parse opens from log
trades = {}
pending_order = {}

with open("auto_trade.log", "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    ts = line[:19]
    if ts < PAPER_START:
        continue

    if "下單:" in line:
        m = re.search(
            r"下單: (\S+) (\w+) units=([\d.]+).*sl=([\d.]+) tp1~tp4=([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
            line,
        )
        if m:
            pending_order[m.group(1)] = {
                "side": m.group(2), "units": float(m.group(3)),
                "sl": float(m.group(4)),
                "tp1": float(m.group(5)), "tp2": float(m.group(6)),
                "tp3": float(m.group(7)), "tp4": float(m.group(8)),
            }

    if "已開倉:" in line and "key=" in line:
        m = re.search(r"已開倉: (\S+) (\w+) \w+ key=(\S+) units=([\d.]+)", line)
        if not m:
            continue
        sym, side, key, units = m.group(1), m.group(2), m.group(3), float(m.group(4))
        order = pending_order.get(sym, {})

        # Find entry price from PaperBroker log nearby
        entry = 0
        for j in range(max(0, i - 10), min(len(lines), i + 10)):
            if "PaperBroker 模擬成交:" in lines[j] and sym in lines[j]:
                m2 = re.search(r"@ ([\d.]+)", lines[j])
                if m2:
                    entry = float(m2.group(1))
                    break

        if key not in trades:
            trades[key] = {
                "symbol": sym, "side": side, "key": key, "units": units,
                "entry": entry,
                "sl": order.get("sl", 0),
                "tp1": order.get("tp1", 0), "tp2": order.get("tp2", 0),
                "tp3": order.get("tp3", 0), "tp4": order.get("tp4", 0),
                "opened": ts,
                "timeframe": "4h" if "P4H" in key else "1h",
            }

print(f"Parsed {len(trades)} trades from log")

# Step 2: Verify each trade against K-line
cfg = load_config()
init_db(cfg["database"]["url"])
symbol_map = cfg.get("market_data", {}).get("symbol_mapping", {})
s = get_session()

results = []
for key, t in sorted(trades.items(), key=lambda x: x[1]["opened"]):
    entry = t["entry"]
    sl, tp1, tp2, tp3, tp4 = t["sl"], t["tp1"], t["tp2"], t["tp3"], t["tp4"]
    is_long = t["side"] == "long"

    if not entry or not sl:
        results.append({**t, "result": "UNKNOWN", "max_tp": 0, "exit_price": 0, "exit_time": ""})
        continue

    try:
        open_time = datetime.strptime(t["opened"][:23], "%Y-%m-%d %H:%M:%S,%f")
    except Exception:
        try:
            open_time = datetime.strptime(t["opened"][:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            results.append({**t, "result": "UNKNOWN", "max_tp": 0, "exit_price": 0, "exit_time": ""})
            continue

    try:
        candles = load_candles(
            s, t["symbol"], t["timeframe"],
            open_time, open_time + timedelta(hours=72), symbol_map=symbol_map,
        )
    except Exception:
        results.append({**t, "result": "NO_DATA", "max_tp": 0, "exit_price": 0, "exit_time": ""})
        continue

    if not candles:
        results.append({**t, "result": "NO_DATA", "max_tp": 0, "exit_price": 0, "exit_time": ""})
        continue

    max_tp_hit = 0
    exit_reason = "OPEN"
    exit_price = 0
    exit_time = ""

    for c in candles:
        h, l = c.high, c.low
        if is_long:
            if tp1 and h >= tp1 and max_tp_hit < 1: max_tp_hit = 1
            if tp2 and h >= tp2 and max_tp_hit < 2: max_tp_hit = 2
            if tp3 and h >= tp3 and max_tp_hit < 3: max_tp_hit = 3
            if tp4 and h >= tp4 and max_tp_hit < 4: max_tp_hit = 4
            if sl and l <= sl:
                exit_reason = "SL" if max_tp_hit == 0 else f"TP{max_tp_hit}+SL"
                exit_price = sl
                exit_time = str(c.open_time)
                break
        else:
            if tp1 and l <= tp1 and max_tp_hit < 1: max_tp_hit = 1
            if tp2 and l <= tp2 and max_tp_hit < 2: max_tp_hit = 2
            if tp3 and l <= tp3 and max_tp_hit < 3: max_tp_hit = 3
            if tp4 and l <= tp4 and max_tp_hit < 4: max_tp_hit = 4
            if sl and h >= sl:
                exit_reason = "SL" if max_tp_hit == 0 else f"TP{max_tp_hit}+SL"
                exit_price = sl
                exit_time = str(c.open_time)
                break

    if exit_reason == "OPEN" and max_tp_hit == 4:
        exit_reason = "TP4"
        exit_price = tp4
    elif exit_reason == "OPEN" and max_tp_hit > 0:
        exit_reason = f"TP{max_tp_hit}_OPEN"

    results.append({**t, "result": exit_reason, "max_tp": max_tp_hit, "exit_price": exit_price, "exit_time": exit_time})

s.close()

# Print
print()
print(f"{'時間':>16} {'幣種':>6} {'方向':>5} {'TF':>3} {'Entry':>10} {'SL':>10} {'結果':>12} {'最高TP':>6}")
print("-" * 80)

tp_count = sl_count = open_count = 0
for r in sorted(results, key=lambda x: x["opened"]):
    sym = r["symbol"].replace("USDT.P", "")
    res = r["result"]
    if res == "SL":
        sl_count += 1
    elif "TP" in res and "OPEN" not in res:
        tp_count += 1
    elif "OPEN" in res:
        open_count += 1
    else:
        sl_count += 1  # UNKNOWN/NO_DATA count as loss
    print(f'{r["opened"][:16]:>16} {sym:>6} {r["side"]:>5} {r["timeframe"]:>3} {r["entry"]:>10.2f} {r["sl"]:>10.2f} {res:>12} TP{r["max_tp"]}')

closed_count = tp_count + sl_count
print(f"\nTP: {tp_count}  SL: {sl_count}  持倉中: {open_count}")
if closed_count:
    print(f"勝率(碰TP1不算SL): {tp_count}/{closed_count} = {tp_count/closed_count*100:.1f}%")

# Save history
for label in ["h1", "h4"]:
    history = []
    for r in results:
        if "OPEN" in r.get("result", ""):
            continue
        is_h4 = r["timeframe"] == "4h"
        if (label == "h4" and is_h4) or (label == "h1" and not is_h4):
            risk = abs(r["entry"] - r["sl"]) if r["entry"] and r["sl"] else 0
            units = r["units"]

            if r["result"] == "SL":
                pnl = -risk * units
            elif r["max_tp"] >= 4:
                pnl3 = abs(r["tp3"] - r["entry"]) * units * 0.5
                pnl4 = abs(r["tp4"] - r["entry"]) * units * 0.5
                pnl = pnl3 + pnl4
            elif r["max_tp"] >= 3:
                pnl3 = abs(r["tp3"] - r["entry"]) * units * 0.5
                pnl_sl = -risk * units * 0.5
                pnl = pnl3 + pnl_sl
            elif "TP" in r["result"] and "SL" in r["result"]:
                pnl = -risk * units  # 碰TP後打SL但沒出場
            else:
                pnl = -risk * units  # default SL

            fee = r["entry"] * units * 0.001
            history.append({
                "signal_key": r["key"], "symbol": r["symbol"], "side": r["side"],
                "entry_price": r["entry"], "sl_original": r["sl"],
                "tp1": r["tp1"], "tp2": r["tp2"], "tp3": r["tp3"], "tp4": r["tp4"],
                "total_units": units, "remaining_units": 0,
                "closed": True, "close_reason": r["result"],
                "max_tp_hit": r["max_tp"],
                "opened_at": r["opened"], "closed_at": r.get("exit_time", ""),
                "realized_pnl": pnl - fee, "fee": fee,
            })

    path = f"db/trade_history_{label}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=str)

    h_pnl = sum(h["realized_pnl"] for h in history)
    h_tp = sum(1 for h in history if "TP" in h["close_reason"] and h["close_reason"] != "SL")
    h_sl = sum(1 for h in history if h["close_reason"] == "SL")
    print(f'{label.upper()}: {len(history)} 筆 (TP={h_tp} SL={h_sl}) PnL=${h_pnl:+.2f}')

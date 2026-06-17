import json, sys
sys.stdout.reconfigure(encoding="utf-8")

all_t = []
for label in ["h1", "h4"]:
    with open(f"db/trade_history_{label}.json", encoding="utf-8") as f:
        for t in json.load(f):
            t["_l"] = label.upper()
            all_t.append(t)

print("=== 碰 TP 但虧損的交易 ===\n")
wrong = []
for t in all_t:
    mtp = t.get("max_tp_hit", 0)
    pnl = t.get("realized_pnl", 0)
    if mtp >= 1 and pnl < -0.1:
        wrong.append(t)

for t in sorted(wrong, key=lambda x: x.get("realized_pnl", 0)):
    sym = t.get("symbol", "").replace("USDT.P", "")
    e = t.get("entry_price", 0)
    sl = t.get("sl_original", 0)
    risk = abs(e - sl)
    units = t.get("total_units", 0)
    mtp = t.get("max_tp_hit", 0)
    pnl = t.get("realized_pnl", 0)
    fee = t.get("fee", 0)
    reason = t.get("close_reason", "")
    tp3 = t.get("tp3", 0)

    if mtp >= 3 and tp3:
        tp3_profit = abs(tp3 - e) * units * 0.5
        expected = tp3_profit - risk * units * 0.5 - fee
    else:
        expected = -risk * units - fee

    diff = pnl - expected
    flag = " <<<" if abs(diff) > 0.5 else ""
    print(f"  {t['_l']} {sym:6s} {t.get('side',''):5s} TP{mtp} PnL=${pnl:+.2f} 預期=${expected:+.2f} 差=${diff:+.2f}{flag} | {reason}")

total_wrong = sum(t.get("realized_pnl", 0) for t in wrong)
print(f"\n碰TP但虧損: {len(wrong)} 筆, 合計 ${total_wrong:+.2f}")

print("\n=== 碰 TP3+ 驗證 ===\n")
for t in all_t:
    mtp = t.get("max_tp_hit", 0)
    if mtp < 3:
        continue
    pnl = t.get("realized_pnl", 0)
    sym = t.get("symbol", "").replace("USDT.P", "")
    e = t.get("entry_price", 0)
    tp3 = t.get("tp3", 0)
    tp4 = t.get("tp4", 0)
    sl = t.get("sl_original", 0)
    risk = abs(e - sl)
    units = t.get("total_units", 0)
    fee = t.get("fee", 0)
    reason = t.get("close_reason", "")

    tp3_profit = abs(tp3 - e) * units * 0.5 if tp3 else 0
    if mtp >= 4:
        tp4_profit = abs(tp4 - e) * units * 0.5 if tp4 else 0
        ideal = tp3_profit + tp4_profit - fee
    else:
        ideal = tp3_profit - risk * units * 0.5 - fee

    diff = pnl - ideal
    flag = " <<< WRONG" if abs(diff) > 0.5 else " ok"
    print(f"  {t['_l']} {sym:6s} TP{mtp} PnL=${pnl:+.2f} 理想=${ideal:+.2f} 差=${diff:+.2f}{flag} | {reason}")

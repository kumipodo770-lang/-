"""
전략별 최소 예산 계산 (읽기만): 실제로 S1 사건이 났던 코인(스크리너 h_history, 읽기 전용)의 바이비트 최소 주문 단위·현재가로
'최소 주문 크기로 들어가도 손절 시 위험 ≤ max_risk_after_rounding_pct(0.5%)' 가 되려면 예산이 얼마 필요한지.
"""
import os
import sys
import json
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.market import Market

HH = r"C:\Users\zzewwz\Desktop\bybit-screener-v9.2.4\bybit-screener\backend\h_history.json"
SL, FT, MAXR, TARGET = 0.15, 0.00055, 0.005, 0.0025

m = Market()
inst = m.instruments()
syms = sorted({e["symbol"] for e in json.load(open(HH, encoding="utf-8"))["events"]})
rows = []
for s in syms:
    i = inst.get(s)
    if not i:
        rows.append((s, None)); continue
    px = m.ticker(s)["last"]
    q = max(i["min_qty"], math.ceil(i["min_notional"] / px / i["qty_step"] - 1e-9) * i["qty_step"])
    notional = q * px
    loss = q * px * SL + q * px * FT + q * px * (1 - SL) * FT
    rows.append((s, notional, loss))
ok = [r for r in rows if r[1]]
print(f"사건 코인 {len(syms)}개, 바이비트 종목 {len(ok)}개")
loss_sorted = sorted(r[2] for r in ok)
for pct in (50, 80, 90, 95, 100):
    L = loss_sorted[min(len(loss_sorted) - 1, int(math.ceil(len(loss_sorted) * pct / 100)) - 1)]
    print(f"코인 {pct}% 를 받으려면: 1다리 최소 손실 {L:.3f} USDT → 예산 ≥ {L / MAXR:.0f} (위험 0.5%) / {L / TARGET:.0f} (위험 0.25%)"
          f" · H4(2다리) ≥ {2 * L / MAXR:.0f} / {2 * L / TARGET:.0f}")
big = sorted(ok, key=lambda r: -r[1])[:6]
print("최소 주문액이 큰 코인:", ", ".join(f"{s} {n:.2f}USDT" for s, n, _ in big))

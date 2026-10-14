"""지금 레짐(K1·변동성)과 바뀌는 선 (공개 시세만). 사용: python tools/regime_now.py"""
import os
import sys
import math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone, timedelta
from app.market import Market
from app.regime import Regime, daily_from_1h

KST = timezone(timedelta(hours=9))
m = Market()
r = Regime(m)
r.refresh(force=True)
d = daily_from_1h(r.bars)
c = [x[1] for x in d]
s50 = sum(c[-50:]) / 50
s50_10 = sum(c[-60:-10]) / 50
day = datetime.fromtimestamp(d[-1][0] / 1000, KST).strftime("%m-%d %H:%M")
print(f"[K1] 판정 {day} KST 마감 일봉 기준")
print(f"  BTC 종가 {c[-1]:,.0f} / 50일 평균 {s50:,.0f} ({(c[-1] / s50 - 1) * 100:+.1f}%) / 50일 평균의 10일 변화 {s50 - s50_10:+,.0f}")
print(f"  → {'상승' if r.k1 == 1 else '하락' if r.k1 == -1 else '횡보'}")
lr = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
rv = []
for j in range(29, len(lr)):
    w = lr[j - 29:j + 1]
    mu = sum(w) / 30
    rv.append(math.sqrt(sum((x - mu) ** 2 for x in w) / 30) * math.sqrt(365) * 100)
past = sorted(rv[-365:])
q33, q67 = past[int(len(past) * 0.33)], past[int(len(past) * 0.67)]
print(f"[변동성] BTC 최근 30일 변동성 연 {rv[-1]:.1f}% → 지난 1년 중 {r.info.get('rv_pct', 0):.0f}백분위")
print(f"  지난 1년 분포: 하위 33% 선 ≈ 연 {q33:.1f}% / 상위 33% 선 ≈ 연 {q67:.1f}% / 최저 {past[0]:.1f}% / 최고 {past[-1]:.1f}%")
print(f"  → {'저변동' if r.k7 == -1 else '고변동' if r.k7 == 1 else '중변동'}")

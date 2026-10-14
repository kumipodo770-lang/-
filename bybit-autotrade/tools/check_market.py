"""공개 시세 연결 점검 (키 없음): 종목 규칙, 현재가, 1분봉, 유지증거금률, 펀딩 이력, 레짐"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.market import Market
from app.regime import Regime
from app.timeutil import in_capture, now_ms

if in_capture():
    print("캡처 시간대 — 나중에 실행"); sys.exit(0)
m = Market()
inst = m.instruments()
print("USDT 무기한 종목 수", len(inst))
for s in ("BTCUSDT", "DOGEUSDT", "MUBARAKUSDT"):
    i = inst.get(s)
    print(s, i, "현재가", m.ticker(s)["last"], "mmr", m.mmr(s))
k = m.klines("DOGEUSDT", "1", start=now_ms() - 5 * 60_000, end=now_ms(), limit=10)
print("1분봉", k[-2:])
print("펀딩", m.funding_history("DOGEUSDT", limit=3))
r = Regime(m); r.refresh(force=True)
print("레짐", r.snapshot())

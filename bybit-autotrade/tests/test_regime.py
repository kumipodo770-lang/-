"""레짐 계산: 연구(regime_lib) 정의와 같은지 — 끝난 날만, K1 규칙, K7 백분위"""
import math
from app.regime import daily_from_1h, labels
from app.timeutil import H1, DAY

CUT = 4 * H1


def bars_from_daily(closes, start_day):
    out = []
    for i, c in enumerate(closes):
        d0 = start_day + i * DAY - DAY            # 그날 봉들 = [d−24h, d)
        for h in range(24):
            out.append((d0 + h * H1, c, c, c, c))
    return out


def test_only_finished_days():
    d0 = 100 * DAY + CUT
    bars = bars_from_daily([1, 2, 3], d0)
    assert [x[0] for x in daily_from_1h(bars, now=d0 + 2 * DAY)] == [d0, d0 + DAY, d0 + 2 * DAY]
    assert [x[0] for x in daily_from_1h(bars, now=d0 + 2 * DAY - 1)] == [d0, d0 + DAY]   # 마지막 날은 아직 안 끝남


def test_k1_up_down_side():
    d0 = 100 * DAY + CUT
    up = [100 + i for i in range(80)]
    k1, k7, info = labels(daily_from_1h(bars_from_daily(up, d0), now=10 ** 15))
    assert k1 == 1
    down = [200 - i for i in range(80)]
    assert labels(daily_from_1h(bars_from_daily(down, d0), now=10 ** 15))[0] == -1
    flat_then_drop = [100] * 70 + [99.9] * 10      # 50일선 아래지만 기울기 음수 약함 → 하락/횡보 판정 규칙대로
    k = labels(daily_from_1h(bars_from_daily(flat_then_drop, d0), now=10 ** 15))[0]
    assert k in (-1, 0)


def test_k7_percentile_low_when_calm():
    d0 = 100 * DAY + CUT
    closes, c = [], 100.0
    for i in range(400):
        c *= math.exp((0.05 if i % 2 else -0.05) if i < 360 else (0.001 if i % 2 else -0.001))
        closes.append(c)
    k1, k7, info = labels(daily_from_1h(bars_from_daily(closes, d0), now=10 ** 15))
    assert k7 == -1 and info["rv_pct"] < 33
